"""
Tests for aria.memory.embeddings (HttpEmbeddings, VoyageEmbeddings, EmbeddingService).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx


# ---------------------------------------------------------------------------
# HttpEmbeddings
# ---------------------------------------------------------------------------

class TestHttpEmbeddings:
    """Tests for HttpEmbeddings."""

    def test_init(self):
        from aria.memory.embeddings import HttpEmbeddings

        emb = HttpEmbeddings(base_url="http://localhost:8001/v1/", model="test-model")
        assert emb.base_url == "http://localhost:8001/v1"
        assert emb.model == "test-model"

    @pytest.mark.asyncio
    async def test_embed_posts_to_endpoint(self):
        from aria.memory.embeddings import HttpEmbeddings

        emb = HttpEmbeddings(base_url="http://localhost:8001/v1", model="test-model")
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.raise_for_status = MagicMock()
        fake_response.json.return_value = {
            "data": [{"embedding": [0.1, 0.2, 0.3]}]
        }
        emb.client = MagicMock()
        emb.client.post = AsyncMock(return_value=fake_response)

        result = await emb.embed("hello world")

        emb.client.post.assert_called_once_with(
            "http://localhost:8001/v1/embeddings",
            json={"input": "hello world", "model": "test-model"},
        )
        assert result == [0.1, 0.2, 0.3]


# ---------------------------------------------------------------------------
# EmbeddingService
# ---------------------------------------------------------------------------

class TestEmbeddingService:
    """Tests for EmbeddingService."""

    def _make_service(self, dimension=1024):
        """Create an EmbeddingService with mocked primary/fallback."""
        with patch("aria.memory.embeddings.settings") as mock_settings:
            mock_settings.embedding_url = "http://localhost:8001/v1"
            mock_settings.embedding_model = "test-model"
            mock_settings.embedding_dimension = dimension
            mock_settings.voyage_api_key = "fake-key"

            from aria.memory.embeddings import EmbeddingService
            service = EmbeddingService()

        # Replace primary and fallback with mocks
        service.primary = MagicMock()
        service.primary.embed = AsyncMock(return_value=[0.1] * dimension)
        service.fallback = MagicMock()
        service.fallback.embed = AsyncMock(return_value=[0.2] * dimension)
        # Bypass circuit breaker: call awaits the async fn
        service.circuit_breaker = MagicMock()

        async def _call_fn(fn):
            return await fn()

        service.circuit_breaker.call = AsyncMock(side_effect=_call_fn)
        return service

    def test_validate_dimension_correct(self):
        service = self._make_service(dimension=4)
        result = service._validate_dimension([0.1, 0.2, 0.3, 0.4])
        assert result == [0.1, 0.2, 0.3, 0.4]

    def test_validate_dimension_mismatch(self):
        service = self._make_service(dimension=4)
        with pytest.raises(ValueError, match="dimension mismatch"):
            service._validate_dimension([0.1, 0.2])

    @pytest.mark.asyncio
    async def test_embed_or_none_success(self):
        service = self._make_service(dimension=4)
        service.primary.embed = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])

        async def _passthrough(fn, **kw):
            return await fn()

        with patch("aria.memory.embeddings.retry_async", side_effect=_passthrough):
            result = await service.embed_or_none("test text")
        assert result == [0.1, 0.2, 0.3, 0.4]

    @pytest.mark.asyncio
    async def test_embed_or_none_failure(self):
        service = self._make_service(dimension=4)
        service.circuit_breaker.call = AsyncMock(side_effect=RuntimeError("boom"))
        service.fallback = None  # no fallback
        result = await service.embed_or_none("test text")
        assert result is None

    @pytest.mark.asyncio
    async def test_embed_batch(self):
        service = self._make_service(dimension=3)
        service.primary.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])

        async def _passthrough(fn, **kw):
            return await fn()

        with patch("aria.memory.embeddings.retry_async", side_effect=_passthrough):
            results = await service.embed_batch(["a", "b", "c"], batch_size=2)
        assert len(results) == 3
        assert all(r == [0.1, 0.2, 0.3] for r in results)
        assert service.primary.embed.call_count == 3

    @pytest.mark.asyncio
    async def test_embed_uses_fallback_on_primary_failure(self):
        service = self._make_service(dimension=4)
        service.circuit_breaker.call = AsyncMock(side_effect=RuntimeError("primary down"))
        service.fallback.embed = AsyncMock(return_value=[0.5, 0.6, 0.7, 0.8])

        async def _passthrough(fn, **kw):
            return await fn()

        with patch("aria.memory.embeddings.retry_async", side_effect=_passthrough):
            result = await service.embed("test text")

        assert result == [0.5, 0.6, 0.7, 0.8]

    @pytest.mark.asyncio
    async def test_embed_force_fallback(self):
        service = self._make_service(dimension=4)
        service.fallback.embed = AsyncMock(return_value=[0.9, 0.8, 0.7, 0.6])
        result = await service.embed("test text", use_fallback=True)
        assert result == [0.9, 0.8, 0.7, 0.6]
        service.primary.embed.assert_not_called()
