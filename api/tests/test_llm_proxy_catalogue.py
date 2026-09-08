"""Explicit-only deployments stay discoverable without becoming automatic routes."""

import json
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
import pytest

from aria.api.routes import llm_proxy
from aria.infrastructure.llm_route import base_url_for, select
from tests.test_llm_proxy_usage import _request


ENGINE = "Qwen3.8-Flash-Next-Engine-R9700-Halo"


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [None, "Qwen3.8-Flash-Next-CUDA-Halo-Candidate"])
async def test_backend_diagnostic_can_inspect_explicit_queue_without_changing_pin(monkeypatch, requested):
    selected = llm_proxy._Route(requested, "http://localhost:8131/v1", "fixture", [])
    pick = AsyncMock(return_value=selected)
    read_pin = AsyncMock(return_value="unchanged-default")
    snapshot = AsyncMock(return_value={"controlled": True, "active": False, "queued": 0})
    monkeypatch.setattr(llm_proxy, "_pick_backend", pick)
    monkeypatch.setattr(llm_proxy, "read_pin", read_pin)
    monkeypatch.setattr(llm_proxy, "_admission_snapshot", snapshot)
    manager, db = MagicMock(), MagicMock()
    response = await llm_proxy.current_backend(manager, db, model=requested)
    pick.assert_awaited_once_with(manager, db, requested=requested)
    snapshot.assert_awaited_once_with(selected)
    assert response["requested_model"] == requested
    assert response["pinned"] == "unchanged-default"
    assert response["admission"]["controlled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [None, "Qwen3.8-Flash-Next-CUDA-Halo-Candidate"])
async def test_identified_backend_diagnostic_preserves_requested_model(monkeypatch, requested):
    expected = {"requested_model": requested, "backend": requested, "pinned": "unchanged-default"}
    handler = AsyncMock(return_value=expected)
    monkeypatch.setattr(llm_proxy, "current_backend", handler)
    manager, db = MagicMock(), MagicMock()
    response = await llm_proxy.current_backend_identified(manager, db, model=requested)
    handler.assert_awaited_once_with(manager, db, model=requested)
    assert response == expected


def server(slug, port, auto_route):
    return {"slug": slug, "port": port, "state": "running", "onbox": True,
            "startable": auto_route, "auto_route": auto_route,
            "endpoints": {"local": f"http://127.0.0.1:{port}/v1"},
            "resident_gib_estimate": 82 if slug == ENGINE else 4}


def route_for(servers, requested=None, pin=None):
    selected, reason, unavailable = select(servers, requested=requested, pin=pin)
    return llm_proxy._Route(selected["slug"] if selected else None,
                            base_url_for(selected) if selected else None,
                            reason, servers, unavailable)


@pytest.mark.asyncio
async def test_candidate_only_catalogue_exposes_exact_slug_without_auto_alias(monkeypatch):
    servers = [server(ENGINE, 8122, False)]
    monkeypatch.setattr(llm_proxy, "_pick_backend", AsyncMock(return_value=route_for(servers)))
    monkeypatch.setattr(llm_proxy, "_context_length", AsyncMock(return_value=262144))
    result = await llm_proxy.list_models(MagicMock(), MagicMock())
    data = json.loads(result.body)
    assert [entry["id"] for entry in data["data"]] == [ENGINE]
    assert data["data"][0]["meta"]["context_length"] == 262144
    assert data["models"] == [{"name": ENGINE, "model": ENGINE}]
    assert data["x_aria_backend"] == {"slug": None, "base_url": None}


@pytest.mark.asyncio
async def test_no_loaded_deployments_retains_catalogue_503(monkeypatch):
    monkeypatch.setattr(llm_proxy, "_pick_backend", AsyncMock(return_value=route_for([])))
    with pytest.raises(HTTPException) as error:
        await llm_proxy.list_models(MagicMock(), MagicMock())
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_auto_catalogue_context_excludes_explicit_only_candidate(monkeypatch):
    servers = [server(ENGINE, 8122, False), server("normal-resident", 8104, True)]
    monkeypatch.setattr(llm_proxy, "_pick_backend", AsyncMock(return_value=route_for(servers)))
    # A smaller experimental allocation must not lower the ordinary alias cap.
    monkeypatch.setattr(llm_proxy, "_context_length", AsyncMock(side_effect=[4096, 32768]))
    result = await llm_proxy.list_models(MagicMock(), MagicMock())
    data = json.loads(result.body)
    alias = data["data"][0]
    assert alias["id"] == "aria-resident"
    assert alias["meta"]["resolves_to"] == "normal-resident"
    assert alias["meta"]["context_length"] == 32768
    assert {entry["id"] for entry in data["data"]} == {"aria-resident", ENGINE, "normal-resident"}


@pytest.mark.asyncio
async def test_deliberate_candidate_pin_advertises_its_available_alias(monkeypatch):
    servers = [server(ENGINE, 8122, False)]
    monkeypatch.setattr(llm_proxy, "_pick_backend", AsyncMock(return_value=route_for(servers, pin=ENGINE)))
    monkeypatch.setattr(llm_proxy, "_context_length", AsyncMock(return_value=262144))
    result = await llm_proxy.list_models(MagicMock(), MagicMock())
    alias = json.loads(result.body)["data"][0]
    assert alias["id"] == "aria-resident"
    assert alias["meta"]["resolves_to"] == ENGINE
    assert alias["meta"]["context_length"] == 262144


@pytest.mark.asyncio
async def test_candidate_only_auto_inference_refuses_but_explicit_request_preserves_timings(monkeypatch):
    servers = [server(ENGINE, 8122, False)]
    async def pick(_manager, _db, requested=None):
        return route_for(servers, requested=requested)
    monkeypatch.setattr(llm_proxy, "_pick_backend", pick)
    monkeypatch.setattr(llm_proxy, "_record_gateway_usage", AsyncMock())
    monkeypatch.setattr(llm_proxy, "_backend_model_id_cached", AsyncMock(return_value="qwen3.8-flash-next-engine"))
    monkeypatch.setattr(llm_proxy.settings, "llm_proxy_autostart", False)
    client = MagicMock()
    payload = {"choices": [{"message": {"content": "fixture"}}],
               "usage": {"prompt_tokens": 100, "completion_tokens": 512,
                         "prompt_tokens_details": {"cached_tokens": 80}},
               "timings": {"prompt_per_second": 1234.0, "predicted_per_second": 55.0}}
    response = MagicMock(status_code=200, headers={"content-type": "application/json"})
    response.json.return_value = payload
    client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(llm_proxy, "_client", lambda: client)
    messages = [{"role": "user", "content": "qualification fixture"}]
    with pytest.raises(HTTPException) as error:
        await llm_proxy._proxy("chat/completions", _request({"model": "aria-resident", "messages": messages}),
                               MagicMock(), MagicMock(), identify=True)
    assert error.value.status_code == 503
    client.post.assert_not_awaited()
    result = await llm_proxy._proxy("chat/completions", _request({"model": ENGINE, "messages": messages}),
                                    MagicMock(), MagicMock(), identify=True)
    assert result.status_code == 200
    assert result.headers["X-Aria-Backend"] == ENGINE
    assert "X-Aria-Queue-Ms" in result.headers
    assert json.loads(result.body) == payload
    forwarded = json.loads(client.post.call_args.kwargs["content"])
    assert forwarded["model"] == "qwen3.8-flash-next-engine"
    assert forwarded["messages"] == messages
