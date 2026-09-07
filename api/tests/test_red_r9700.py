"""Red's replacement hardware must never inherit a healthy legacy proxy identity."""
import httpx
import pytest

from aria.infrastructure import model_servers as ms


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
    assert not spec.auto_route
    assert spec.remotely_operable
