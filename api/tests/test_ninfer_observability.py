"""
Observability for ninfer-serve, and registry statements that must stay true.

ninfer-serve (the Corsair RTX 3090 deployment) exposes /v1/models and /health
and nothing else. Its family used to probe as None, which the utilization
endpoint reports as `reachable: false`, so a healthy serving model read as
unobserved and the Operate verdict raised it as a finding. These tests pin:

* reachability proven by identity (owned_by=ninfer), not by an open port;
* occupancy UNKNOWN from the runtime, filled only from the gateway's own
  admission queue, and never invented where no queue exists;
* Halogen probing unchanged (qualification work runs there);
* registry role statements that previously contradicted each other.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from aria.api.routes import llm_proxy
from aria.infrastructure import model_servers as ms
from aria.infrastructure.model_servers import LaunchGeometry, RuntimeStats, _BY_SLUG

NINFER = "NInfer-3090-Qwen3.8-27B"


class _Client:
    """httpx.AsyncClient stand-in returning one canned /v1/models response."""
    def __init__(self, response=None, raises=None):
        self._response, self._raises = response, raises
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url):
        if self._raises: raise self._raises
        return self._response


def _resp(status, body=None):
    r = MagicMock(status_code=status)
    r.json.return_value = body
    return r


def _patch_client(monkeypatch, **kw):
    monkeypatch.setattr(ms.httpx, "AsyncClient", lambda *a, **k: _Client(**kw))
    monkeypatch.setattr(ms, "_read_observed_launch_geometry", lambda spec: LaunchGeometry(n_ctx=96256))


# ------------------------------------------------------------------ probe --

@pytest.mark.asyncio
async def test_ninfer_is_reachable_with_unknown_occupancy(monkeypatch):
    _patch_client(monkeypatch, response=_resp(200, {"data": [{"id": "qwen3.8-27b", "owned_by": "ninfer"}]}))
    st = await ms.probe_runtime(_BY_SLUG[NINFER])
    assert st is not None and st.runtime_family == "ninfer"
    assert st.total_slots == 1                     # declared, runtime-enforced
    assert st.served_ctx == 96256
    assert st.busy_slots is None and st.saturated is None   # unknown, never zero
    assert st.metrics_available is False
    assert "404" in st.telemetry_hint and "gateway" in st.telemetry_hint


@pytest.mark.asyncio
async def test_a_different_server_on_the_port_is_not_ninfer(monkeypatch):
    """:8080 also hosted retired R9700 bundles; a 200 is not proof of identity."""
    _patch_client(monkeypatch, response=_resp(200, {"data": [{"id": "x", "owned_by": "vllm"}]}))
    assert await ms.probe_runtime(_BY_SLUG[NINFER]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    {"response": _resp(503, {})},
    {"response": _resp(200, "not-json-object")},
    {"raises": httpx.ConnectError("refused")},
])
async def test_unreachable_or_malformed_stays_unreachable(monkeypatch, kw):
    _patch_client(monkeypatch, **kw)
    assert await ms.probe_runtime(_BY_SLUG[NINFER]) is None


@pytest.mark.asyncio
async def test_halogen_probing_is_unchanged(monkeypatch):
    """Halogen deployments are under active qualification; this change must not
    alter what is reported for them."""
    called = AsyncMock()
    monkeypatch.setattr(ms.httpx, "AsyncClient", lambda *a, **k: called)
    import dataclasses
    halogen = dataclasses.replace(_BY_SLUG[NINFER], slug="halogen-like", runtime_family="halogen")
    assert await ms.probe_runtime(halogen) is None
    called.assert_not_called()


# ------------------------------------------------------ gateway occupancy --

@pytest.mark.asyncio
async def test_admission_occupancy_never_creates_a_queue(monkeypatch):
    monkeypatch.setattr(llm_proxy, "_admissions", {})
    assert await llm_proxy.admission_occupancy("http://127.0.0.1:8080/v1") is None
    assert llm_proxy._admissions == {}
    assert await llm_proxy.admission_occupancy(None) is None


@pytest.mark.asyncio
async def test_admission_occupancy_reads_an_existing_queue(monkeypatch):
    queue = llm_proxy._PriorityAdmission(aging_seconds=30)
    monkeypatch.setattr(llm_proxy, "_admissions", {"http://127.0.0.1:8080/v1": queue})
    await queue.acquire(2)
    snap = await llm_proxy.admission_occupancy("http://127.0.0.1:8080/v1")
    assert snap["active"] is True and snap["queued"] == 0
    await queue.release()


async def _utilization_row(monkeypatch, *, stats: RuntimeStats, queue: dict | None, slots=1):
    from aria.api.routes import infrastructure
    server = {"slug": NINFER, "state": "running", "slots": slots}
    manager = MagicMock()
    manager.status = AsyncMock(return_value=[server])
    monkeypatch.setattr(infrastructure, "is_servable", lambda s: True)
    monkeypatch.setattr(infrastructure, "probe_runtime", AsyncMock(return_value=stats))
    monkeypatch.setattr(infrastructure, "check_pi_slot_budget", lambda: None)
    monkeypatch.setattr(llm_proxy, "admission_occupancy", AsyncMock(return_value=queue))
    result = await infrastructure.model_server_utilization(manager=manager, db=MagicMock())
    return result["servers"][0]


@pytest.mark.asyncio
async def test_utilization_fills_ninfer_occupancy_from_the_gateway(monkeypatch):
    row = await _utilization_row(
        monkeypatch,
        stats=RuntimeStats(runtime_family="ninfer", total_slots=1),
        queue={"active": True, "queued": 2, "queued_by_priority": {"interactive": 1, "foreground": 0, "background": 1}},
    )
    assert row["reachable"] is True
    assert row["busy_slots"] == 1 and row["requests_deferred"] == 2
    assert row["saturated"] is True
    assert row["occupancy_source"] == "gateway-admission"


@pytest.mark.asyncio
async def test_utilization_leaves_occupancy_unknown_without_a_queue(monkeypatch):
    row = await _utilization_row(monkeypatch, stats=RuntimeStats(runtime_family="ninfer", total_slots=1), queue=None)
    assert row["reachable"] is True
    assert row["busy_slots"] is None and row["saturated"] is None
    assert row["occupancy_source"] is None


@pytest.mark.asyncio
async def test_runtime_reported_occupancy_is_never_overridden(monkeypatch):
    row = await _utilization_row(
        monkeypatch,
        stats=RuntimeStats(runtime_family="llamacpp", total_slots=1, busy_slots=0, requests_deferred=0),
        queue={"active": True, "queued": 5, "queued_by_priority": {}},
    )
    assert row["busy_slots"] == 0 and row["occupancy_source"] is None


@pytest.mark.asyncio
async def test_multi_slot_backends_are_not_overlaid(monkeypatch):
    row = await _utilization_row(monkeypatch, stats=RuntimeStats(runtime_family="vllm", total_slots=8),
                                 queue={"active": True, "queued": 1, "queued_by_priority": {}}, slots=8)
    assert row["busy_slots"] is None and row["occupancy_source"] is None


# --------------------------------------------------------------- registry --

def test_pi_slot_budget_names_a_registered_deployment():
    """It has pointed at a retired or stopped slug three times; an unknown slug
    makes the check report 'cannot be checked' forever."""
    assert ms.PI_CODING_SLUG == "Red-Qwen3.8-27B-PARO-INT5"
    assert ms.PI_CODING_SLUG in _BY_SLUG
    assert "cannot be checked" not in (ms.check_pi_slot_budget() or "")


def test_only_the_current_default_calls_itself_the_default():
    """The stopped CUDA/Halo candidate kept describing itself as the Hermes and Pi
    default after PARO-INT5 took over, contradicting two other entries."""
    import re

    def claims_default(note: str) -> bool:
        # Affirmative claims only: "not a Pi or Hermes default" is a correct
        # statement by an explicit alternative, not a competing claim.
        for sentence in re.split(r"[.;]", note.lower()):
            if re.search(r"hermes default|explicit default for managed", sentence) and not re.search(r"\bnot\b", sentence):
                return True
        return False

    claims = sorted(s.slug for s in ms.REGISTRY if claims_default(s.consumers_note or ""))
    assert claims == ["Red-Qwen3.8-27B-PARO-INT5"]


def test_explicit_alternatives_do_not_claim_automatic_fallback():
    for s in ms.REGISTRY:
        note = (s.consumers_note or "").lower()
        if not s.auto_route and "qualified automatic fallback" in note:
            pytest.fail(f"{s.slug} is not auto-routed but claims to be an automatic fallback")
