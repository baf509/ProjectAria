"""NInfer-3090 Qwen3.8-27B: static registration on the Corsair RTX 3090.

It shares :8080 with two retired R9700 entries, so identity must come from the
backend's own /v1/models ownership. It is explicit-selection only, so Red keeps
the model-omitted fallback, and it is exclusive with the CUDA/Halo candidate,
which also allocates on the 3090.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aria.infrastructure import corsair_actuator
from aria.infrastructure import model_servers as ms
from aria.infrastructure.gpu_devices import POOL_NVIDIA
from aria.infrastructure.llm_route import select
from aria.infrastructure.model_servers import ModelServerManager

SLUG = "NInfer-3090-Qwen3.8-27B"
CANDIDATE = "Qwen3.8-Flash-Next-CUDA-Halo-Candidate"


def test_registration_describes_the_live_3090_deployment():
    spec = ms._BY_SLUG[SLUG]
    assert spec.onbox and spec.startable and not spec.allow_force_start
    assert spec.port == 8080
    assert spec.systemd_unit == "ninfer-3090.service"
    assert ms.unit_name(spec) == "ninfer-3090.service"
    assert spec.memory_pool == POOL_NVIDIA
    assert spec.runtime_family == "ninfer"
    assert spec.auto_route is False
    assert ms.base_url_for_spec(spec) == "http://127.0.0.1:8080/v1"


def test_exclusive_with_the_candidate_in_both_directions():
    assert CANDIDATE in ms._BY_SLUG[SLUG].exclusive_with
    assert SLUG in ms._BY_SLUG[CANDIDATE].exclusive_with


def test_backend_ownership_identifies_ninfer():
    assert ms._runtime_family_from_models(
        {"data": [{"id": "qwen3.8-27b", "owned_by": "ninfer"}]}
    ) == "ninfer"


@pytest.mark.asyncio
async def test_shared_port_8080_resolves_to_ninfer_not_the_retired_r9700_entries():
    specs = [ms._BY_SLUG[s] for s in (SLUG, "Qwen3.8-27B-R9700-Radiance", "Qwen3.8-27B-R9700-HIP")]

    async def fake_probe(port, *, identify_runtime=False):
        assert port == 8080 and identify_runtime is True
        return True, "ninfer"

    with patch.object(ms, "_forwarded_endpoint_status", side_effect=fake_probe):
        states = await ms._forwarded_fleet_states(specs)
    assert states[SLUG] == "running"
    assert states["Qwen3.8-27B-R9700-Radiance"] == "exited"
    assert states["Qwen3.8-27B-R9700-HIP"] == "exited"


def test_running_ninfer_is_selectable_by_slug_but_never_the_default():
    ninfer = {"slug": SLUG, "state": "running", "onbox": True, "port": 8080,
              "auto_route": False, "resident_gib_estimate": 19.7, "model_file": ms._BY_SLUG[SLUG].model_file,
              "endpoints": {"local": "http://127.0.0.1:8080/v1"}}
    red = {"slug": "Red-Qwen3.8-27B-MXFP4", "state": "running", "onbox": False, "port": 8094,
           "remote_identity_verified": True, "auto_route": True, "resident_gib_estimate": 63.5,
           "endpoints": {"local": "http://127.0.0.1:8094/v1"}}
    chosen, _, _ = select([ninfer, red])
    assert chosen["slug"] == "Red-Qwen3.8-27B-MXFP4"
    chosen, reason, _ = select([ninfer, red], requested=SLUG)
    assert chosen["slug"] == SLUG and "requested" in reason
    chosen, _, _ = select([ninfer])
    assert chosen is None


def test_restricted_corsair_actuator_accepts_the_slug():
    request = corsair_actuator.parse_request(f"start {SLUG}", ModelServerManager())
    assert (request.action, request.slug, request.force) == ("start", SLUG, False)


@pytest.mark.asyncio
async def test_ninfer_is_not_probed_as_llamacpp():
    # ninfer-serve 404s llama.cpp's /slots and /metrics, so it must never be
    # probed that way. It has its own identity-checked probe (2026-09-15);
    # both are patched so this stays hermetic rather than reaching a live :8080.
    with patch.object(ms, "_probe_llamacpp", AsyncMock()) as llamacpp, \
         patch.object(ms, "_probe_ninfer", AsyncMock(return_value=None)) as ninfer:
        assert await ms.probe_runtime(ms._BY_SLUG[SLUG]) is None
    llamacpp.assert_not_awaited()
    ninfer.assert_awaited_once()
