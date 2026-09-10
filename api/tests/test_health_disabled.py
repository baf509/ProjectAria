from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from aria.api.routes import health


@pytest.mark.parametrize("enabled", [False, True])
async def test_health_respects_embedding_switch_without_hiding_outages(monkeypatch, enabled):
    from aria.memory.capabilities import retrieval_capabilities
    monkeypatch.setattr(retrieval_capabilities._embeddings, "enabled", enabled)
    monkeypatch.setattr(health.llm_manager, "is_backend_available", lambda *a: (True, "configured"))
    calls = []

    async def get(_self, url, **kwargs):
        calls.append(url)
        if url.endswith("/health"):
            raise httpx.ConnectError("disabled service")
        return MagicMock(status_code=200)

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    result = await health.health_check(depth="deep", db=MagicMock(command=AsyncMock()))
    assert result.embeddings == ("unreachable" if enabled else "disabled")
    assert result.status == ("degraded" if enabled else "healthy")
    assert any(url.endswith("/health") for url in calls) is enabled
