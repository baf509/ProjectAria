"""
Background inference on single-slot deployments: priority admission and
reasoning effort.

The Corsair RTX 3090 serves one request at a time and is shared by Hermes cron,
vision, and ARIA's background workers. Two things had to be true before those
workers could be pointed at it, and these tests pin both:

1. The gateway must SEE it as single-slot, or priority admission never engages
   and a burst of background extraction competes first-come-first-served with
   Hermes. NInfer exposes no -np flag and its unit lives on Corsair, so the
   slot count has to come from the registry.
2. ARIA's workers must not spend the slot reasoning. They send no reasoning
   control, and this model reasons by default.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.api.routes import llm_proxy
from aria.infrastructure import model_servers as ms
from aria.infrastructure.model_servers import LaunchGeometry, _BY_SLUG

NINFER = "NInfer-3090-Qwen3.8-27B"


# --------------------------------------------------------------- geometry --

class TestDeclaredSlots:

    def test_ninfer_declares_its_runtime_enforced_single_slot(self):
        spec = _BY_SLUG[NINFER]
        assert spec.declared_slots == 1
        assert spec.background_reasoning_effort == "none"

    def test_declared_slots_fill_in_when_nothing_observable_answers(self, monkeypatch):
        monkeypatch.setattr(ms, "_read_observed_launch_geometry",
                            lambda spec: LaunchGeometry(n_ctx=96256, slots=None, source=None))
        geometry = ms.read_launch_geometry(_BY_SLUG[NINFER])
        assert geometry.slots == 1
        assert geometry.n_ctx == 96256          # observed context is kept
        assert "declared" in geometry.source

    def test_observed_geometry_always_wins_over_a_declaration(self, monkeypatch):
        """A declaration is a fallback. If the launch file is readable and says
        otherwise, the launch file is the truth — or an edited unit would be
        silently misreported."""
        monkeypatch.setattr(ms, "_read_observed_launch_geometry",
                            lambda spec: LaunchGeometry(n_ctx=4096, slots=4, source="unit"))
        geometry = ms.read_launch_geometry(_BY_SLUG[NINFER])
        assert geometry.slots == 4
        assert geometry.source == "unit"

    def test_undeclared_specs_are_unchanged(self, monkeypatch):
        monkeypatch.setattr(ms, "_read_observed_launch_geometry",
                            lambda spec: LaunchGeometry())
        undeclared = [s for s in _BY_SLUG.values() if not s.declared_slots]
        assert undeclared, "expected most specs to declare nothing"
        assert all(ms.read_launch_geometry(s).slots is None for s in undeclared)

    def test_only_ninfer_declares_background_reasoning_today(self):
        # A new entry opting in should be a deliberate, reviewed choice.
        opted_in = sorted(s.slug for s in _BY_SLUG.values() if s.background_reasoning_effort)
        assert opted_in == [NINFER]


# -------------------------------------------------------------- admission --

def test_ninfer_status_row_engages_gateway_priority_admission(monkeypatch):
    """End to end through the real row builder: registry declaration -> status
    row -> the gateway's single-slot check. This chain is what was broken."""
    monkeypatch.setattr(llm_proxy.settings, "llm_proxy_admission_enabled", True)
    monkeypatch.setattr(ms, "_read_observed_launch_geometry", lambda spec: LaunchGeometry())
    spec = _BY_SLUG[NINFER]
    row = ms._server_row(spec, "running", ms.read_launch_geometry(spec), {}, None, {}, {}, {})
    assert row["slots"] == 1

    route = llm_proxy._Route(NINFER, "http://127.0.0.1:8080/v1", "explicit", [row])
    assert llm_proxy._route_slots(route) == 1
    assert llm_proxy._admission_for(route) is not None


# -------------------------------------------------------- reasoning effort --

class TestBackgroundReasoningEffort:

    @pytest.mark.parametrize("caller", ["aria-background", "steward-worker", "evalstack-benchmark"])
    def test_background_callers_get_the_registry_effort(self, caller):
        assert llm_proxy._caller_priority(caller) == 2
        assert llm_proxy._background_reasoning_effort(NINFER, caller, {"messages": []}) == "none"

    @pytest.mark.parametrize("caller", ["hermes", "hermes-cron", "hermes-vision"])
    def test_hermes_is_never_altered(self, caller):
        """Hermes cron and vision share this slot and chose their own settings."""
        assert llm_proxy._caller_priority(caller) == 0
        assert llm_proxy._background_reasoning_effort(NINFER, caller, {"messages": []}) is None

    def test_foreground_coding_is_never_altered(self):
        assert llm_proxy._background_reasoning_effort(NINFER, "pi-coding-mac", {"messages": []}) is None

    def test_an_explicit_reasoning_effort_wins(self):
        body = {"messages": [], "reasoning_effort": "xhigh"}
        assert llm_proxy._background_reasoning_effort(NINFER, "aria-background", body) is None

    @pytest.mark.parametrize("thinking", [True, False])
    def test_an_explicit_enable_thinking_wins_either_way(self, thinking):
        body = {"messages": [], "chat_template_kwargs": {"enable_thinking": thinking}}
        assert llm_proxy._background_reasoning_effort(NINFER, "aria-background", body) is None

    def test_models_that_did_not_opt_in_are_untouched(self):
        other = next(s.slug for s in _BY_SLUG.values() if not s.background_reasoning_effort)
        assert llm_proxy._background_reasoning_effort(other, "aria-background", {"messages": []}) is None

    def test_unrouted_or_unknown_model_is_untouched(self):
        assert llm_proxy._background_reasoning_effort(None, "aria-background", {}) is None
        assert llm_proxy._background_reasoning_effort("no-such-model", "aria-background", {}) is None


def _request(body: dict, caller: str):
    import json
    from starlette.requests import Request
    raw = json.dumps(body).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request({
        "type": "http", "method": "POST", "path": "/llm/v1-identified/chat/completions",
        "headers": [(b"user-agent", b"test/1.0"), (b"x-aria-caller", caller.encode())],
        "query_string": b"", "client": ("127.0.0.1", 1234),
    }, receive)


async def _forwarded_body(monkeypatch, caller: str, body: dict) -> dict:
    import json
    from tests.conftest import make_mock_db
    route = llm_proxy._Route(NINFER, "http://127.0.0.1:8080/v1", "explicit", [])
    monkeypatch.setattr(llm_proxy, "_pick_backend", AsyncMock(return_value=route))
    monkeypatch.setattr(llm_proxy, "_backend_model_id_cached", AsyncMock(return_value="qwen3.8-27b"))
    response = MagicMock(status_code=200, headers={"content-type": "application/json"}, text="")
    response.json.return_value = {"choices": [{"message": {"content": "[]"}}],
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(llm_proxy, "_client", lambda: client)
    await llm_proxy._proxy("chat/completions", _request(body, caller), MagicMock(), make_mock_db())
    return json.loads(client.post.call_args.kwargs["content"])


@pytest.mark.asyncio
async def test_proxy_forwards_reasoning_none_for_aria_background_work(monkeypatch):
    sent = await _forwarded_body(monkeypatch, "aria-background",
                                 {"model": NINFER, "messages": [{"role": "user", "content": "x"}]})
    assert sent["reasoning_effort"] == "none"
    assert sent["model"] == "qwen3.8-27b"          # the existing rewrite still happens


@pytest.mark.asyncio
async def test_proxy_forwards_hermes_requests_unchanged(monkeypatch):
    sent = await _forwarded_body(monkeypatch, "hermes-cron",
                                 {"model": NINFER, "messages": [{"role": "user", "content": "x"}]})
    assert "reasoning_effort" not in sent
