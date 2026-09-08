"""Red's replacement hardware must never inherit a healthy legacy proxy identity."""
import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from aria.api.routes import llm_proxy
from aria.infrastructure import model_servers as ms
from aria.infrastructure.llm_route import select


@pytest.mark.asyncio
@pytest.mark.parametrize("health,models,body,expected", [
    (200, 200, {"data": [{"id": "qwen3.8-27b"}]}, True),
    (200, 200, {"data": [{"id": "qwen3.6-35b-a3b"}]}, False),
    (200, 200, {"data": []}, False),
    (200, 503, {"data": [{"id": "qwen3.8-27b"}]}, False),
    (404, 200, {"data": [{"id": "qwen3.8-27b"}]}, False),
    (401, 200, {"data": [{"id": "qwen3.8-27b"}]}, False),
    (503, 200, {"data": [{"id": "qwen3.8-27b"}]}, False),
    (200, 200, {"data": None}, False),
    (200, 200, ["qwen3.8-27b"], False),
])
async def test_red_readiness_requires_health_and_identity(monkeypatch, health, models, body, expected):
    spec = ms.ModelServerManager().get_spec("Red-Qwen3.8-27B-MXFP4")
    client_type = httpx.AsyncClient

    def handle(request):
        if request.url.path == "/health":
            return httpx.Response(health)
        assert request.url.path == "/v1/models"
        return httpx.Response(models, json=body)

    monkeypatch.setattr(ms.httpx, "AsyncClient", lambda **kw: client_type(
        transport=httpx.MockTransport(handle), **kw))
    assert await ms._remote_health_ok(spec) is expected


@pytest.mark.asyncio
async def test_retired_red_cannot_start_even_with_force():
    manager = ms.ModelServerManager()
    old = manager.get_spec("Red-Qwen3.6-35B-A3B")
    assert not old.startable and not old.auto_route and not old.remotely_operable
    with pytest.raises(ms.ModelServerSafetyError, match="no remote start/stop"):
        await manager.start(old.slug, force=True)


def test_red_runtime_is_exact_and_loopback_only():
    spec = ms.ModelServerManager().get_spec("Red-Qwen3.8-27B-MXFP4")
    assert spec.runtime_repo == "https://codeberg.org/ggz14/radiance-vllm-mxfp4"
    assert spec.runtime_family == "vllm"
    assert spec.endpoint_override == "http://127.0.0.1:8094/v1"
    assert spec.remote_model_id == "qwen3.8-27b"
    assert spec.auto_route
    assert spec.remotely_operable
    assert ms.unit_name(spec) is None  # Lifecycle is remote; never run systemctl on the Mac.


def test_only_identified_remote_is_explicitly_routable():
    row = {"slug": "Red-Qwen3.8-27B-MXFP4", "state": "running", "onbox": False,
           "port": 8094, "endpoints": {"local": "http://127.0.0.1:8094/v1"},
           "remote_identity_verified": True, "auto_route": False}
    assert select([row], requested=row["slug"])[0] == row
    assert select([row])[0] is None
    for overrides in [{"remote_identity_verified": False}, {"state": "stopped"}]:
        assert select([{**row, **overrides}], requested=row["slug"])[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy", [True, False])
async def test_named_red_gets_fresh_identity_confirmation(monkeypatch, healthy):
    row = {"slug": "Red-Qwen3.8-27B-MXFP4", "state": "stopped", "onbox": False,
           "port": 8094, "endpoints": {"local": "http://127.0.0.1:8094/v1"},
           "remote_identity_required": True, "remote_identity_verified": False,
           "auto_route": False}
    monkeypatch.setattr(llm_proxy, "_running_summary_cached", AsyncMock(return_value=[row]))
    monkeypatch.setattr(llm_proxy, "read_pin", AsyncMock(return_value=None))
    manager = MagicMock()
    manager.confirm_forwarded_resident = AsyncMock(return_value=healthy)
    route = await llm_proxy._pick_backend(manager, MagicMock(), requested=row["slug"])
    manager.confirm_forwarded_resident.assert_awaited_once()
    assert route.slug == (row["slug"] if healthy else None)
    assert not row["remote_identity_verified"]  # No stale-positive cache mutation.


@pytest.mark.asyncio
async def test_catalogue_reads_vllm_context(monkeypatch):
    response = MagicMock()
    response.json.return_value = {"data": [{"id": "qwen3.8-27b", "max_model_len": 262144}]}
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    monkeypatch.setattr(llm_proxy, "_client", lambda: client)
    assert await llm_proxy._context_length("http://127.0.0.1:8094/v1") == 262144
