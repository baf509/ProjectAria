from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from aria.infrastructure import backend_auth as auth
from aria.api.routes import llm_proxy as proxy


@pytest.fixture
def private_key(tmp_path, monkeypatch):
    path = tmp_path / "key"
    path.write_text("s" * 64 + "\n")
    path.chmod(0o600)
    monkeypatch.setattr(auth, "credential_path", lambda: path)
    return path


def test_only_exact_registered_loopback_scope_receives_key(private_key):
    for base in ("http://127.0.0.1:8131", "http://localhost:8131/v1", "http://localhost:8131/v1/"):
        assert auth.backend_headers(base) == {"Authorization": "Bearer " + "s" * 64}
    for base in ("https://localhost:8131/v1", "http://corsair-ai:8131/v1", "http://127.0.0.1:8132/v1",
                 "http://localhost:8131/llm/v1", "http://localhost:8131/v1?secret=x",
                 "http://localhost:8131/v1#fragment", "http://user@localhost:8131/v1",
                 "http://localhost.evil:8131/v1", "http://[::1]:8131/v1",
                 "http://localhost:bad/v1", "http://[localhost:8131/v1"):
        assert auth.backend_headers(base) == {}


def test_private_key_missing_permissions_symlink_and_content_fail_closed(private_key):
    private_key.chmod(0o644)
    with pytest.raises(auth.BackendAuthError):
        auth.read_private_key(private_key)
    private_key.chmod(0o600)
    for value in ("short", "x" * 64 + "\n" + "y" * 32, "x" * 200, "secret value " * 4):
        private_key.write_text(value)
        with pytest.raises(auth.BackendAuthError) as error:
            auth.read_private_key(private_key)
        assert value not in str(error.value)
    private_key.write_text("s" * 64)
    link = private_key.with_name("link")
    link.symlink_to(private_key)
    with pytest.raises(auth.BackendAuthError):
        auth.read_private_key(link)
    with pytest.raises(auth.BackendAuthError):
        auth.read_private_key(private_key.with_name("missing"))


def test_key_never_forwards_caller_admin_or_inference_authority(private_key):
    request = Request({"type": "http", "headers": [
        (b"authorization", b"Bearer private-admin"), (b"x-api-key", b"private-other"),
        (b"content-type", b"application/json")]})
    headers = proxy._headers_for_backend(request, "http://localhost:8131/v1")
    assert headers == {"content-type": "application/json", "Authorization": "Bearer " + "s" * 64}
    private_key.unlink()
    with pytest.raises(proxy.HTTPException) as error:
        proxy._headers_for_backend(request, "http://localhost:8131/v1")
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_context_probe_authenticates_and_missing_key_is_not_a_context(private_key, monkeypatch):
    response = httpx.Response(200, json={"data": [{"meta": {"n_ctx": 262144}}]})
    client = AsyncMock()
    client.get.return_value = response
    monkeypatch.setattr(proxy, "_client", lambda: client)
    assert await proxy._context_length("http://localhost:8131/v1") == 262144
    assert client.get.call_args.kwargs["headers"]["Authorization"] == "Bearer " + "s" * 64
    private_key.unlink()
    client.get.reset_mock()
    assert await proxy._context_length("http://localhost:8131/v1") is None
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_identity_and_slot_metrics_probes_use_model_only_key(private_key, monkeypatch):
    from aria.infrastructure import llm_route, model_servers
    requests = []

    async def respond(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + "s" * 64
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "candidate-fixture"}]})
        if request.url.path == "/slots":
            return httpx.Response(200, json=[{"id": 0, "is_processing": False, "n_ctx": 262144}])
        return httpx.Response(200, text="llamacpp:requests_processing 0\nllamacpp:requests_deferred 0\n")

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
                        original(**kwargs, transport=httpx.MockTransport(respond)))
    assert await llm_route.backend_model_id("http://localhost:8131/v1") == "candidate-fixture"
    spec = next(s for s in model_servers.REGISTRY if s.slug == "Qwen3.8-Flash-Next-CUDA-Halo-Candidate")
    assert await model_servers._probe_llamacpp(spec, "http://127.0.0.1:8131", 1) is not None
    assert {r.url.path for r in requests} == {"/v1/models", "/slots", "/metrics"}


@pytest.mark.asyncio
async def test_missing_auth_does_not_enter_autostart_readiness_loop(private_key, monkeypatch):
    private_key.unlink()
    client = AsyncMock()
    monkeypatch.setattr(proxy, "_client", lambda: client)
    assert await proxy._await_ready("http://localhost:8131/v1", "candidate") is False
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_generation_path_replaces_caller_credentials(private_key, monkeypatch):
    from unittest.mock import MagicMock
    from tests.test_llm_proxy_usage import _request
    route = proxy._Route("candidate", "http://localhost:8131/v1", "fixture", [])
    monkeypatch.setattr(proxy, "_pick_backend", AsyncMock(return_value=route))
    monkeypatch.setattr(proxy, "_record_gateway_usage", AsyncMock())
    monkeypatch.setattr(proxy, "_backend_model_id_cached", AsyncMock(return_value="candidate-fixture"))
    client = MagicMock()
    response = httpx.Response(200, json={"choices": [{"message": {"content": "fixture"}}]})
    client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(proxy, "_client", lambda: client)
    request = _request({"model": "candidate", "messages": [{"role": "user", "content": "fixture"}]})
    request.scope["headers"] = [(b"authorization", b"Bearer private-admin"), (b"x-api-key", b"private-other")]
    result = await proxy._proxy("chat/completions", request, MagicMock(), MagicMock(), identify=False)
    assert result.status_code == 200
    headers = client.post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer " + "s" * 64
    assert "private-admin" not in str(headers) and "private-other" not in str(headers)
