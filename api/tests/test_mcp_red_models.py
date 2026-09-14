"""Red MCP selection: real schema, lifecycle failures and nondestructive refusals."""
import asyncio
from copy import deepcopy
import json

import pytest
from types import SimpleNamespace
from tests.test_mcp_operations import operations


class Fleet:
    def __init__(self, module):
        self.module = module
        self.rows = {slug: dict(slug=slug, state="stopped", startable=True,
                               catalog_visible=True, bound_agents=[])
                     for slug in module.RED_MODELS.values()}
        self.old = module.RED_MODELS["qwen3.8-27b"]
        self.new = module.RED_MODELS["qwen-flash-next"]
        self.pinned = None
        self.busy = self.queued = 0
        self.unknown = False
        self.stop_fails = self.stop_stalls = self.start_stalls = False
        self.mutate_binding = False
        self.reads = 0
        self.posts = []

    async def request(self, method, path, **kwargs):
        if method == "GET":
            if path.endswith("/llm-route"):
                return {"pinned": self.pinned}
            if path.endswith("/utilization"):
                return {"servers": [{"slug": self.old, "reachable": not self.unknown,
                    "busy_slots": self.busy, "requests_processing": self.busy, "requests_deferred": 0}]}
            if path == "/llm/v1/backend":
                slug = kwargs["params"]["model"]
                return {"backend": slug if self.rows[slug]["state"] == "running" else None,
                        "admission": {"active": False, "queued": self.queued}}
            self.reads += 1
            if self.mutate_binding and self.reads > 2:
                self.rows[self.old]["bound_agents"] = ["new-assignment"]
            return deepcopy(self.rows[path.rsplit("/", 1)[-1]])
        assert method == "POST"
        slug, action = path.split("/")[-2:]
        self.posts.append((slug, action))
        if action == "stop":
            if self.stop_fails:
                raise RuntimeError("private-upstream-error")
            if not self.stop_stalls:
                self.rows[slug]["state"] = "stopped"
            return {"state": "stopped"}
        assert action == "start" and kwargs["json"] == {"force": False}
        self.rows[slug]["state"] = "starting" if self.start_stalls else "running"
        return {"state": "starting" if self.start_stalls else "ready", "woken": True}


@pytest.fixture
def setup_red(operations, monkeypatch, tmp_path):
    module, server, request = operations
    monkeypatch.setenv("ARIA_MCP_STATE_DIR", str(tmp_path / "locks"))
    fleet = Fleet(module)
    request.side_effect = fleet.request
    select = server._tool_manager.get_tool("select_red_model").fn
    return module, server, fleet, select


@pytest.mark.asyncio
async def test_schema_only_offers_supported_choices_and_readonly_status(setup_red):
    _, server, fleet, _ = setup_red
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["select_red_model"].inputSchema
    # `force` is offered; `ctx` is injected by the server, not part of the schema.
    assert set(schema["properties"]) == {"model", "force"}
    assert set(schema["properties"]["model"]["enum"]) == {"qwen3.8-27b", "qwen-flash-next", "qwen3.8-27b-paro-int5"}
    assert tools["red_model_status"].annotations.readOnlyHint is True
    with pytest.raises(Exception):
        await server.call_tool("select_red_model", {"model": "gemma"})
    assert fleet.reads == 0 and fleet.posts == []
    status = await server._tool_manager.get_tool("red_model_status").fn()
    assert len(status["models"]) == 3 and fleet.posts == []


@pytest.mark.asyncio
async def test_wake_load_verify_then_idempotent_noop(setup_red):
    _, _, f, select = setup_red
    for r in f.rows.values():
        r["state"] = "asleep"
    result = await select("qwen-flash-next")
    assert result["status"] == "ready" and result["request_model"] == f.new
    assert result["woken"] and not result["routing_changed"] and not result["hermes_model_changed"]
    assert f.posts == [(f.new, "start")]
    assert (await select("qwen-flash-next"))["status"] == "ready"
    assert f.posts == [(f.new, "start")]


@pytest.mark.asyncio
async def test_switch_stops_before_starting_and_preserves_corsair_pin(setup_red):
    _, _, f, select = setup_red
    f.rows[f.old]["state"] = "running"
    f.pinned = "Qwen3.8-Flash-Next-CUDA-Halo-Candidate"
    result = await select("qwen-flash-next")
    assert result["status"] == "ready" and result["previous_slug"] == f.old
    assert f.posts == [(f.old, "stop"), (f.new, "start")]


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["busy", "queued", "unknown", "pinned", "assigned", "missing_assignments", "binding_race", "loading", "unknown_state", "retired", "two_residents"])
async def test_refusals_never_stop_or_start(setup_red, condition):
    _, _, f, select = setup_red
    f.rows[f.old]["state"] = "running"
    if condition == "busy": f.busy = 1
    if condition == "queued": f.queued = 1
    if condition == "unknown": f.unknown = True
    if condition == "pinned": f.pinned = f.old
    if condition == "assigned": f.rows[f.old]["bound_agents"] = ["pi-coding-red-qwen38-27b"]
    if condition == "missing_assignments": f.rows[f.old].pop("bound_agents")
    if condition == "binding_race": f.mutate_binding = True
    if condition == "loading": f.rows[f.old]["state"] = "starting"
    if condition == "unknown_state": f.rows[f.old]["state"] = "unknown"
    if condition == "retired": f.rows[f.new]["startable"] = False
    if condition == "two_residents": f.rows[f.new]["state"] = "running"
    assert (await select("qwen-flash-next"))["status"] in ("blocked", "pending")
    assert f.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["busy", "assigned"])
async def test_force_requires_consent_and_declining_stops_nothing(setup_red, condition):
    """force is a prompt, not a bypass: declining must leave Red untouched."""
    _, _, f, select = setup_red
    f.rows[f.old]["state"] = "running"
    if condition == "busy": f.busy = 1
    if condition == "assigned": f.rows[f.old]["bound_agents"] = ["pi-coding-red-qwen38-27b"]

    class Declines:
        async def elicit(self, message, schema):
            self.message = message
            return SimpleNamespace(action="decline")

    ctx = Declines()
    assert (await select("qwen-flash-next", ctx=ctx, force=True))["status"] == "cancelled"
    assert f.posts == []
    assert "Red" in ctx.message


@pytest.mark.asyncio
async def test_force_with_consent_interrupts_and_switches(setup_red):
    """Accepting must actually get through the guard that would otherwise refuse."""
    _, _, f, select = setup_red
    f.rows[f.old]["state"] = "running"
    f.busy = 1

    class Accepts:
        async def elicit(self, message, schema):
            self.message = message
            return SimpleNamespace(action="accept")

    ctx = Accepts()
    result = await select("qwen-flash-next", ctx=ctx, force=True)
    assert result["status"] != "blocked"
    assert [p for p in f.posts if "stop" in p], f.posts
    # The prompt has to say what is being destroyed, not just ask.
    assert "lost" in ctx.message


@pytest.mark.asyncio
async def test_force_without_a_consent_capable_client_is_refused(setup_red):
    _, _, f, select = setup_red
    f.rows[f.old]["state"] = "running"
    f.busy = 1
    with pytest.raises(RuntimeError, match="consent"):
        await select("qwen-flash-next", force=True)
    assert f.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stop_fails", "stop_stalls", "start_stalls"])
async def test_partial_failures_are_not_success_and_do_not_restart_old_model(setup_red, failure):
    _, _, f, select = setup_red
    f.rows[f.old]["state"] = "running"
    setattr(f, failure, True)
    result = await select("qwen-flash-next")
    assert result["status"] in ("error", "pending")
    assert "private-upstream-error" not in json.dumps(result)
    assert f.posts == ([(f.old, "stop"), (f.new, "start")] if failure == "start_stalls" else [(f.old, "stop")])


@pytest.mark.asyncio
async def test_competing_mcp_selection_returns_pending_without_requests(setup_red):
    module, _, f, select = setup_red
    with module._red_selection_lock() as locked:
        assert locked
        assert (await select("qwen-flash-next"))["status"] == "pending"
    assert f.reads == 0 and f.posts == []


@pytest.mark.asyncio
async def test_cancellation_is_propagated_and_releases_selection_lock(setup_red):
    module, _, f, select = setup_red
    from unittest.mock import patch
    with patch.object(module, "_red_rows", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await select("qwen-flash-next")
    with module._red_selection_lock() as locked:
        assert locked


@pytest.mark.asyncio
async def test_start_http_failure_returns_stop_and_recheck_guidance_without_retry(setup_red):
    module, server, fleet, select = setup_red
    class FailedStart(RuntimeError):
        status_code = 500
    original = fleet.request
    async def fail(method, path, **kwargs):
        if method == 'POST':
            fleet.posts.append((fleet.new, 'start'))
            raise FailedStart('private body must not escape')
        return await original(method, path, **kwargs)
    result = await module._select_red(fail, 'qwen-flash-next')
    assert result['status'] == 'error' and result['http_status'] == 500
    assert result['next_tool'] == 'red_model_status'
    assert result['automatic_retry_allowed'] is False
    assert result['shell_fallback_allowed'] is False
    assert fleet.posts == [(fleet.new, 'start')]
    assert 'private body' not in json.dumps(result)
    with module._red_selection_lock():
        pending = await select('qwen-flash-next')
    assert pending['automatic_retry_allowed'] is False
    assert pending['shell_fallback_allowed'] is False


@pytest.mark.asyncio
async def test_switch_to_promoted_paro_stops_existing_27b_first(setup_red):
    module, _, fleet, select = setup_red
    fleet.rows[fleet.old]['state'] = 'running'
    result = await select('qwen3.8-27b-paro-int5')
    target = module.RED_MODELS['qwen3.8-27b-paro-int5']
    assert result['status'] == 'ready' and result['request_model'] == target
    assert fleet.posts == [(fleet.old, 'stop'), (target, 'start')]
