"""Tests for aria.infrastructure.model_servers — the local LLM model-server
control plane. Docker calls and the GTT sysfs read are mocked; agent binding
uses a tiny in-memory fake Mongo collection."""
from __future__ import annotations

from typing import Any, Optional
from unittest.mock import patch

import pytest
from bson import ObjectId

from aria.infrastructure import model_servers as ms
from aria.infrastructure.model_servers import (
    ModelServerBindingConflict,
    ModelServerError,
    ModelServerManager,
    ModelServerNotFound,
    ModelServerSafetyError,
)


# ─────────────────────────────────────────────────────────── fake mongo ──

def _match(doc: dict, query: dict) -> bool:
    for k, v in query.items():
        if isinstance(v, dict):
            if "$ne" in v and doc.get(k) == v["$ne"]:
                return False
            if "$exists" in v and (k in doc) != v["$exists"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, query: dict) -> Optional[dict]:
        for d in self.docs:
            if _match(d, query):
                return d
        return None

    async def update_one(self, query: dict, update: dict):
        for d in self.docs:
            if _match(d, query):
                for k, v in update.get("$set", {}).items():
                    d[k] = v
                for k in update.get("$unset", {}):
                    d.pop(k, None)
                return

    def find(self, query: dict) -> FakeCursor:
        return FakeCursor([d for d in self.docs if _match(d, query)])


class FakeDB:
    def __init__(self):
        self.agents = FakeCollection()
        self.model_servers = FakeCollection()
        self.model_pulls = FakeCollection()


def _agent(slug: str, model_server: Optional[str] = None) -> dict:
    doc = {"_id": ObjectId(), "slug": slug}
    if model_server is not None:
        doc["model_server"] = model_server
    return doc


# ────────────────────────────────────────────────────────── docker fake ──

class FakeDocker:
    """Simulates `docker inspect/start/stop/compose` for _run().

    container_states maps name -> (state, compose_project) where
    compose_project is "" for hand-run containers.
    """

    def __init__(self, container_states: Optional[dict[str, tuple[str, str]]] = None):
        self.container_states = dict(container_states or {})
        self.calls: list[tuple] = []

    async def __call__(self, *args: str):
        if args[:2] == ("docker", "inspect"):
            name = args[-1]
            info = self.container_states.get(name)
            if info is None:
                return 1, "", "Error: No such object: " + name
            state, project = info
            return 0, f"{state}|{project}\n", ""
        if args[:2] == ("docker", "start"):
            self.calls.append(("start", args[2]))
            state, project = self.container_states[args[2]]
            self.container_states[args[2]] = ("running", project)
            return 0, "", ""
        if args[:2] == ("docker", "stop"):
            self.calls.append(("stop", args[2]))
            state, project = self.container_states[args[2]]
            self.container_states[args[2]] = ("exited", project)
            return 0, "", ""
        if args[:2] == ("docker", "compose"):
            self.calls.append(("compose", args))
            # Register the started service's container as running so a later
            # inspect (e.g. the second of two racing starts) observes it —
            # service name == container name for every registry entry.
            service = args[-1]
            self.container_states[service] = ("running", service)
            return 0, "created\n", ""
        return 1, "", f"unhandled: {args}"


class DaemonDownDocker:
    """Every docker invocation fails as if dockerd is unreachable."""

    async def __call__(self, *args: str):
        return 1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"


@pytest.fixture(autouse=True)
def _never_touch_real_docker():
    """Hard stop against the suite shelling out to the real docker daemon.

    Not hypothetical: on 2026-07-30 `test_start_unstartable_without_force_raises`
    called manager.start() without mocking _run, relying on the unstartable
    gate to raise before any subprocess. The moment Chadrockv2 became startable
    that gate stopped firing, the test fell through, and it really ran
    `docker compose up -d chadrockv2` — starting a 27 GiB model server from a
    unit test. Tests that want docker behaviour patch _run themselves; this
    default makes the un-patched path fail loudly instead of escaping.
    """
    def _forbidden(*args, **kwargs):
        raise AssertionError(
            f"test attempted a real subprocess: {' '.join(args)!r}. "
            "Patch aria.infrastructure.model_servers._run in this test."
        )
    with patch.object(ms, "_run", _forbidden):
        yield


@pytest.fixture
def manager():
    return ModelServerManager()


# ───────────────────────────────────────────────────────────── registry ──

def test_registry_has_no_duplicate_slugs():
    slugs = [s.slug for s in ms.REGISTRY]
    assert len(slugs) == len(set(slugs))


def test_unknown_slug_raises_not_found(manager):
    with pytest.raises(ModelServerNotFound):
        manager.get_spec("does-not-exist")


def test_exclusivity_is_symmetric():
    """start() only consults the starting spec's own exclusive_with, so every
    pair must be present in both directions."""
    by_slug = {s.slug: s for s in ms.REGISTRY}
    for spec in ms.REGISTRY:
        for other in spec.exclusive_with:
            assert other in by_slug, f"{spec.slug} exclusive with unknown {other}"
            assert spec.slug in by_slug[other].exclusive_with, (
                f"{spec.slug} -> {other} not mirrored"
            )


def test_qwen_pair_is_not_mutually_exclusive():
    """qwen3.6-35b-a3b-Q4 + qwen3.6-27b-Q8 are designed to start together
    (`--profile qwen`, ~61 GiB pair) — the registry must not forbid it."""
    by_slug = {s.slug: s for s in ms.REGISTRY}
    assert "qwen3.6-27b-Q8" not in by_slug["qwen3.6-35b-a3b-Q4"].exclusive_with
    assert "qwen3.6-35b-a3b-Q4" not in by_slug["qwen3.6-27b-Q8"].exclusive_with


def test_chadrock_qwen_coexistence_not_forbidden():
    """The deliberate two-server split (measured ~89.4 GiB combined)."""
    by_slug = {s.slug: s for s in ms.REGISTRY}
    assert "ROCmFP4-qwen3.6-35b-a3b" not in by_slug["Chadrock-Laguna-S-2.1"].exclusive_with


def test_laguna_rocmfp4_qwen_pair_is_forbidden():
    """87+29 SWAG exceeds the safety margin; the live GTT gate alone would
    currently allow it, so the static pair must catch it."""
    by_slug = {s.slug: s for s in ms.REGISTRY}
    assert "ROCmFP4-qwen3.6-35b-a3b" in by_slug["Laguna-S-2.1"].exclusive_with


# ─────────────────────────────────────────────────────────────── status ──

@pytest.mark.asyncio
async def test_status_reports_live_docker_state(manager):
    docker = FakeDocker({"chadrock": ("exited", ""), "gemma-aux": ("running", "gemma-aux")})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(25.0, 124.0)):
        results = await manager.status()

    by_slug = {r["slug"]: r for r in results}
    assert by_slug["Chadrock-Laguna-S-2.1"]["state"] == "exited"
    assert by_slug["gemma-4-e4b-Q4"]["state"] == "running"
    assert by_slug["Laguna-S-2.1"]["state"] == "not_created"  # not in container_states
    # Chadrockv2 is wired as of 2026-07-30 (compose service + container), so it
    # reports not_created like any other absent container. The synthetic-spec
    # tests below still cover the "unwired"/unstartable branches.
    assert by_slug["Chadrock-ROCmFP6-qwen3.6-27b"]["state"] == "not_created"
    assert by_slug["Ridge-Qwen3.6-35B-A3B"]["state"] == "external"  # onbox=False
    assert by_slug["gemma-4-e4b-Q4"]["gtt_used_gib"] == 25.0


@pytest.mark.asyncio
async def test_status_includes_bound_agents(manager):
    db = FakeDB()
    db.agents.docs.append(_agent("search-agent", model_server="context1-Q4"))
    with patch.object(ms, "_run", FakeDocker()), patch.object(ms, "_read_gtt_gib", return_value=None):
        results = await manager.status(db)
    by_slug = {r["slug"]: r for r in results}
    assert by_slug["context1-Q4"]["bound_agents"] == ["search-agent"]
    assert by_slug["Laguna-S-2.1"]["bound_agents"] == []


@pytest.mark.asyncio
async def test_status_raises_when_daemon_down(manager):
    """A daemon outage must NOT masquerade as an all-not_created fleet."""
    with patch.object(ms, "_run", DaemonDownDocker()), patch.object(ms, "_read_gtt_gib", return_value=None):
        with pytest.raises(ModelServerError, match="daemon"):
            await manager.status()


# ────────────────────────────────────────────────────────────── start() ──

@pytest.mark.asyncio
async def test_start_refuses_on_exclusivity_conflict(manager):
    # Laguna-S-2.1 is exclusive with Chadrock-Laguna-S-2.1; mark chadrock running.
    docker = FakeDocker({"chadrock": ("running", "")})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(20.0, 124.0)):
        with pytest.raises(ModelServerSafetyError, match="mutually exclusive"):
            await manager.start("Laguna-S-2.1")
    assert not docker.calls  # never got as far as issuing a start/compose command


@pytest.mark.asyncio
async def test_start_exclusivity_counts_paused_containers(manager):
    """A paused container's process is frozen with its GTT allocations intact —
    it must conflict the same as a running one."""
    docker = FakeDocker({"chadrock": ("paused", "")})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(20.0, 124.0)):
        with pytest.raises(ModelServerSafetyError, match="chadrock.*paused|Chadrock.*paused"):
            await manager.start("Laguna-S-2.1")


@pytest.mark.asyncio
async def test_start_refuses_on_ram_swag_overflow(manager):
    # chadrock (60 GiB SWAG, exclusive only with laguna which is absent):
    # 112 + 60 = 172 > 0.92 * 124 — exercises the GTT gate alone.
    docker = FakeDocker({})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(112.0, 124.0)):
        with pytest.raises(ModelServerSafetyError, match="safety margin"):
            await manager.start("Chadrock-Laguna-S-2.1")
    assert not docker.calls


@pytest.mark.asyncio
async def test_start_skips_gtt_gate_for_cpu_only_server(manager):
    """gemma is CPU-only (gtt_resident=False): its allocations never hit the
    GTT pool, so even a nearly-full GTT must not refuse it."""
    docker = FakeDocker({})  # not created -> compose up
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(120.0, 124.0)):
        result = await manager.start("gemma-4-e4b-Q4")
    assert result["action"] == "started"


@pytest.mark.asyncio
async def test_start_force_bypasses_safety_checks(manager):
    docker = FakeDocker({"chadrock": ("running", "")})  # would normally conflict
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(112.0, 124.0)):
        result = await manager.start("Laguna-S-2.1", force=True)
    assert result["action"] == "started"
    assert any(kind == "compose" for kind, *_ in docker.calls)


@pytest.mark.asyncio
async def test_start_handrun_container_uses_raw_docker_start_with_note(manager):
    """A hand-run container (no compose label) can't be adopted by compose up
    (name conflict) — raw docker start, with an explicit config-drift note."""
    docker = FakeDocker({"gemma-aux": ("exited", "")})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(10.0, 124.0)):
        result = await manager.start("gemma-4-e4b-Q4")
    assert result["action"] == "started"
    assert docker.calls == [("start", "gemma-aux")]
    assert "compose-file changes are NOT applied" in result["note"]


@pytest.mark.asyncio
async def test_start_compose_managed_container_uses_compose_up(manager):
    """An existing compose-managed container goes through compose up -d so a
    compose-file edit is reconciled instead of resurrecting the old argv."""
    docker = FakeDocker({"gemma-aux": ("exited", "gemma-aux")})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(10.0, 124.0)):
        result = await manager.start("gemma-4-e4b-Q4")
    assert result["action"] == "started"
    assert "note" not in result
    assert len(docker.calls) == 1
    kind, args = docker.calls[0]
    assert kind == "compose"
    assert "up" in args and "gemma-aux" in args


@pytest.mark.asyncio
async def test_start_uses_compose_up_when_container_missing(manager):
    docker = FakeDocker({})  # chadrock doesn't exist yet
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(10.0, 124.0)):
        result = await manager.start("Chadrock-Laguna-S-2.1")
    assert result["action"] == "started"
    assert len(docker.calls) == 1
    kind, args = docker.calls[0]
    assert kind == "compose"
    assert "--profile" in args and "chadrock" in args


@pytest.mark.asyncio
async def test_start_noop_when_already_running_even_if_gates_would_fail(manager):
    """The noop check must come BEFORE the safety gates: an already-running
    server's memory is already counted in GTT-used, and its exclusive peers
    being up is a pre-existing condition, not a new hazard."""
    docker = FakeDocker({"laguna": ("running", "laguna"), "chadrock": ("running", "")})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(120.0, 124.0)):
        result = await manager.start("Laguna-S-2.1")
    assert result == {"slug": "Laguna-S-2.1", "state": "running", "action": "noop"}
    assert not docker.calls


@pytest.mark.asyncio
async def test_start_paused_container_raises_clear_error(manager):
    docker = FakeDocker({"gemma-aux": ("paused", "gemma-aux")})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(10.0, 124.0)):
        with pytest.raises(ModelServerError, match="paused"):
            await manager.start("gemma-4-e4b-Q4")
    assert not docker.calls


# Every real onbox entry is startable as of 2026-07-30, so the unstartable
# branches are exercised with a synthetic spec rather than by pinning a test to
# whichever model happens to be un-wired that week.
_UNWIRED = ms.ModelServerSpec(
    slug="synthetic-unwired",
    description="test-only",
    runtime_repo="", runtime_ref="", backend_device="",
    startable=False,
    not_startable_reason="No compose service exists yet; needs a build.",
)


@pytest.fixture
def unwired_registry():
    # get_spec reads _BY_SLUG; status() walks REGISTRY — patch both.
    with patch.dict(ms._BY_SLUG, {"synthetic-unwired": _UNWIRED}), \
         patch.object(ms, "REGISTRY", ms.REGISTRY + (_UNWIRED,)):
        yield


@pytest.mark.asyncio
async def test_start_unstartable_without_force_raises(manager, unwired_registry):
    with pytest.raises(ModelServerSafetyError, match="No compose service"):
        await manager.start("synthetic-unwired")


@pytest.mark.asyncio
async def test_start_unstartable_with_force_still_fails_no_compose_service(manager, unwired_registry):
    # force bypasses the startable gate, but there's still no compose_file
    # configured, so it must fail loudly rather than silently no-op.
    docker = FakeDocker({})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(10.0, 124.0)):
        with pytest.raises(ModelServerSafetyError, match="no compose service configured"):
            await manager.start("synthetic-unwired", force=True)


@pytest.mark.asyncio
async def test_status_reports_unwired_for_container_less_spec(manager, unwired_registry):
    with patch.object(ms, "_run", FakeDocker()), patch.object(ms, "_read_gtt_gib", return_value=None):
        results = await manager.status()
    assert {r["slug"]: r["state"] for r in results}["synthetic-unwired"] == "unwired"


@pytest.mark.asyncio
async def test_start_offbox_server_refused(manager):
    with pytest.raises(ModelServerSafetyError, match="off-box"):
        await manager.start("Ridge-Qwen3.6-35B-A3B")


@pytest.mark.asyncio
async def test_concurrent_exclusive_starts_only_one_wins(manager):
    """Two concurrent mutually-exclusive starts must not both pass the gates:
    the lock serializes them, so the second observes the first's container
    (FakeDocker registers it on compose up) and refuses."""
    import asyncio

    docker = FakeDocker({})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(5.0, 124.0)):
        results = await asyncio.gather(
            manager.start("Laguna-S-2.1"),
            manager.start("Chadrock-Laguna-S-2.1"),
            return_exceptions=True,
        )
    started = [r for r in results if isinstance(r, dict) and r.get("action") == "started"]
    refused = [r for r in results if isinstance(r, ModelServerSafetyError)]
    assert len(started) == 1, f"expected exactly one winner, got {results}"
    assert len(refused) == 1, f"expected exactly one refusal, got {results}"
    assert len([c for c in docker.calls if c[0] == "compose"]) == 1


# ─────────────────────────────────────────────────────────────── stop() ──

@pytest.mark.asyncio
async def test_stop_noop_when_not_created(manager):
    docker = FakeDocker({})
    with patch.object(ms, "_run", docker):
        result = await manager.stop("Laguna-S-2.1")
    assert result["action"] == "noop"
    assert not docker.calls


@pytest.mark.asyncio
async def test_stop_calls_docker_stop_when_running(manager):
    docker = FakeDocker({"gemma-aux": ("running", "gemma-aux")})
    with patch.object(ms, "_run", docker):
        result = await manager.stop("gemma-4-e4b-Q4")
    assert result["action"] == "stopped"
    assert docker.calls == [("stop", "gemma-aux")]


@pytest.mark.asyncio
async def test_stop_raises_when_daemon_down_instead_of_false_success(manager):
    """dockerd unreachable must NOT be reported as 'not_created' -> noop
    'success' while the server is actually still running."""
    with patch.object(ms, "_run", DaemonDownDocker()):
        with pytest.raises(ModelServerError, match="daemon"):
            await manager.stop("gemma-4-e4b-Q4")


@pytest.mark.asyncio
async def test_stop_offbox_server_refused(manager):
    with pytest.raises(ModelServerSafetyError, match="off-box"):
        await manager.stop("Ridge-Qwen3.6-35B-A3B")


# ──────────────────────────────────────────────────── bind() / unbind() ──

@pytest.mark.asyncio
async def test_bind_sets_model_server_on_agent(manager):
    db = FakeDB()
    agent = _agent("search-agent")
    db.agents.docs.append(agent)

    result = await manager.bind(db, "context1-Q4", "search-agent")
    assert result["model_server"] == "context1-Q4"
    assert agent["model_server"] == "context1-Q4"


@pytest.mark.asyncio
async def test_bind_conflict_without_force_raises(manager):
    db = FakeDB()
    db.agents.docs.append(_agent("agent-a", model_server="context1-Q4"))
    db.agents.docs.append(_agent("agent-b"))

    with pytest.raises(ModelServerBindingConflict, match="agent-a"):
        await manager.bind(db, "context1-Q4", "agent-b")


@pytest.mark.asyncio
async def test_bind_conflict_with_force_adds_extra_slot(manager):
    db = FakeDB()
    a = _agent("agent-a", model_server="context1-Q4")
    b = _agent("agent-b")
    db.agents.docs.extend([a, b])

    result = await manager.bind(db, "context1-Q4", "agent-b", force=True)
    assert result["extra_slot"] is True
    assert a["model_server"] == "context1-Q4"
    assert b["model_server"] == "context1-Q4"


@pytest.mark.asyncio
async def test_bind_unknown_agent_raises(manager):
    db = FakeDB()
    with pytest.raises(ModelServerNotFound, match="Unknown agent"):
        await manager.bind(db, "context1-Q4", "no-such-agent")


@pytest.mark.asyncio
async def test_bind_unknown_slug_raises(manager):
    db = FakeDB()
    db.agents.docs.append(_agent("agent-a"))
    with pytest.raises(ModelServerNotFound, match="Unknown model server"):
        await manager.bind(db, "no-such-slug", "agent-a")


@pytest.mark.asyncio
async def test_unbind_clears_field(manager):
    db = FakeDB()
    agent = _agent("search-agent", model_server="context1-Q4")
    db.agents.docs.append(agent)

    result = await manager.unbind(db, "search-agent")
    assert result["model_server"] is None
    assert "model_server" not in agent


# ─────────────────────────────── dynamic (pulled) entries + pull service ──

from aria.infrastructure.model_pull import (  # noqa: E402
    RUNTIME_TEMPLATES,
    ModelPullService,
    _compose_yaml,
)


# model_servers/model_pulls now live on the base FakeDB; alias kept for the
# dynamic-entry tests' readability.
FakeDBWithServers = FakeDB


def _dynamic_doc(slug="pulled-qwen", port=8105):
    return {
        "slug": slug,
        "description": "Pulled from hf.co/org/repo",
        "runtime_repo": "ghcr.io/ggml-org/llama.cpp:server-vulkan",
        "runtime_ref": "template mainline-vulkan",
        "backend_device": "Vulkan0",
        "model_file": f"models/llm/{slug}/model.gguf",
        "port": port,
        "compose_file": f"generated/{slug}/docker-compose.yml",
        "service_name": slug,
        "container_name": slug,
        "resident_gib": 20.0,
        "gtt_resident": True,
    }


@pytest.mark.asyncio
async def test_resolve_spec_finds_dynamic_entry(manager):
    db = FakeDBWithServers()
    db.model_servers.docs.append(_dynamic_doc())
    spec = await manager.resolve_spec("pulled-qwen", db)
    assert spec.port == 8105
    assert spec.compose_file == "generated/pulled-qwen/docker-compose.yml"
    assert spec.exclusive_with == ()


@pytest.mark.asyncio
async def test_resolve_spec_unknown_still_raises_with_db(manager):
    db = FakeDBWithServers()
    with pytest.raises(ModelServerNotFound):
        await manager.resolve_spec("nope", db)


@pytest.mark.asyncio
async def test_status_merges_dynamic_entries(manager):
    db = FakeDBWithServers()
    db.model_servers.docs.append(_dynamic_doc())
    docker = FakeDocker({"pulled-qwen": ("running", "pulled-qwen")})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=None):
        results = await manager.status(db)
    by_slug = {r["slug"]: r for r in results}
    assert by_slug["pulled-qwen"]["state"] == "running"
    assert len(results) == len(ms.REGISTRY) + 1


@pytest.mark.asyncio
async def test_start_dynamic_entry_uses_its_compose_file(manager):
    db = FakeDBWithServers()
    db.model_servers.docs.append(_dynamic_doc())
    docker = FakeDocker({})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(10.0, 124.0)):
        result = await manager.start("pulled-qwen", db=db)
    assert result["action"] == "started"
    kind, args = docker.calls[0]
    assert kind == "compose"
    assert any("generated/pulled-qwen/docker-compose.yml" in a for a in args)


@pytest.mark.asyncio
async def test_bind_accepts_dynamic_slug(manager):
    db = FakeDBWithServers()
    db.model_servers.docs.append(_dynamic_doc())
    db.agents.docs.append(_agent("some-agent"))
    result = await manager.bind(db, "pulled-qwen", "some-agent")
    assert result["model_server"] == "pulled-qwen"


class TestPullValidation:
    @pytest.mark.asyncio
    async def test_rejects_bad_repo(self):
        svc = ModelPullService()
        with pytest.raises(ModelServerError, match="repo id"):
            await svc._validate(FakeDBWithServers(), "not a repo", "m.gguf", "ok-name", "mainline-cpu", None)

    @pytest.mark.asyncio
    async def test_rejects_bad_filename(self):
        svc = ModelPullService()
        with pytest.raises(ModelServerError, match="filename"):
            await svc._validate(FakeDBWithServers(), "org/repo", "../evil.gguf", "ok-name", "mainline-cpu", None)
        with pytest.raises(ModelServerError, match="filename"):
            await svc._validate(FakeDBWithServers(), "org/repo", "model.bin", "ok-name", "mainline-cpu", None)

    @pytest.mark.asyncio
    async def test_rejects_bad_name_and_unknown_runtime(self):
        svc = ModelPullService()
        with pytest.raises(ModelServerError, match="Invalid name"):
            await svc._validate(FakeDBWithServers(), "org/repo", "m.gguf", "has spaces", "mainline-cpu", None)
        with pytest.raises(ModelServerError, match="Unknown runtime"):
            await svc._validate(FakeDBWithServers(), "org/repo", "m.gguf", "ok-name", "cuda", None)

    @pytest.mark.asyncio
    async def test_rejects_duplicate_slug_static_and_dynamic(self):
        svc = ModelPullService()
        with pytest.raises(ModelServerError, match="already exists"):
            await svc._validate(FakeDBWithServers(), "org/repo", "m.gguf", "gemma-4-e4b-Q4", "mainline-cpu", None)
        db = FakeDBWithServers()
        db.model_servers.docs.append(_dynamic_doc(slug="taken"))
        with pytest.raises(ModelServerError, match="already exists"):
            await svc._validate(db, "org/repo", "m.gguf", "taken", "mainline-cpu", None)

    @pytest.mark.asyncio
    async def test_port_allocation_skips_used_ports(self):
        svc = ModelPullService()
        db = FakeDBWithServers()
        db.model_servers.docs.append(_dynamic_doc(slug="taken", port=8107))
        port = await svc._validate(db, "org/repo", "m.gguf", "ok-name", "mainline-cpu", None)
        # 8105/8106 are static (chadrockv2, qwythos) and the dynamic fixture
        # takes 8107, so allocation lands on the next free port.
        assert port == 8108
        with pytest.raises(ModelServerError, match="already assigned"):
            await svc._validate(db, "org/repo", "m.gguf", "ok-name", "mainline-cpu", 8103)  # static qwen port


def test_compose_yaml_generation_per_runtime():
    for runtime, t in RUNTIME_TEMPLATES.items():
        yml = _compose_yaml("my-model", runtime, "my-model.gguf", 8110, 32768)
        assert f"image: {t['image']}" in yml
        assert "container_name: my-model" in yml
        assert '"127.0.0.1:8110:8080"' in yml
        assert "/models/my-model.gguf" in yml
        assert f'- "{t["ngl"]}"' in yml
        if t["entrypoint"]:
            assert t["entrypoint"] in yml
        if t["gpu"] == "vulkan":
            assert "/dev/dri" in yml
        else:
            assert "/dev/dri" not in yml


# ─────────────────────────────────────────────────────────────── sleep() ──

class SleepFakeSSH:
    """Simulates the two ssh invocations sleep() makes: the reachability
    probe (`... ridge exit`) and the suspend command."""

    def __init__(self, reachable: bool):
        self.reachable = reachable
        self.calls: list[tuple] = []

    async def __call__(self, *args: str):
        self.calls.append(args)
        if not self.reachable:
            return 255, "", "ssh: connect to host ridge port 22: Connection timed out"
        if args[-1] == "exit":
            return 0, "", ""
        # Suspend drops the connection — nonzero exit is the success shape.
        return 255, "", "Connection to ridge closed by remote host."


@pytest.mark.asyncio
async def test_sleep_ridge_sends_suspend_when_reachable(manager):
    ssh = SleepFakeSSH(reachable=True)
    with patch.object(ms, "_run", ssh):
        result = await manager.sleep("Ridge-Qwen3.6-35B-A3B")
    assert result["action"] == "sleep_requested"
    assert len(ssh.calls) == 2  # probe + suspend
    assert "SetSuspendState" in ssh.calls[1][-1]


@pytest.mark.asyncio
async def test_sleep_noop_when_already_asleep(manager):
    ssh = SleepFakeSSH(reachable=False)
    with patch.object(ms, "_run", ssh):
        result = await manager.sleep("Ridge-Qwen3.6-35B-A3B")
    assert result == {
        "slug": "Ridge-Qwen3.6-35B-A3B", "state": "asleep", "action": "noop",
        "detail": "unreachable over ssh — already asleep",
    }
    assert len(ssh.calls) == 1  # probe only, no suspend attempt


@pytest.mark.asyncio
async def test_sleep_refused_for_onbox_server(manager):
    with pytest.raises(ModelServerSafetyError, match="no sleep command"):
        await manager.sleep("gemma-4-e4b-Q4")
