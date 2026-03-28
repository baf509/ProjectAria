"""
ARIA - Embedding Service

Phase: 2
Purpose: Generate embeddings via local sentence-transformers service (with Voyage AI cloud fallback)

Related Spec Sections:
- Section 3.4: Embedding Service
"""

import asyncio
from typing import Optional
import logging

import httpx

from aria.config import settings
from aria.core.resilience import CircuitBreaker, retry_async

logger = logging.getLogger(__name__)


class HttpEmbeddings:
    """
    Embedding generation via an OpenAI-compatible /v1/embeddings endpoint.
    """

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(timeout=10.0)

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for text."""
        url = f"{self.base_url}/embeddings"
        logger.debug("Requesting embedding from %s with model %s", url, self.model)
        try:
            response = await self.client.post(
                url,
                json={"input": text, "model": self.model},
            )
            response.raise_for_status()
            data = response.json()
            embedding = data["data"][0]["embedding"]
            logger.debug("Success! Got embedding with %d dimensions", len(embedding))
            return embedding
        except Exception as e:
            logger.error("Embedding error: %s: %s", type(e).__name__, e)
            raise

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


class VoyageEmbeddings:
    """
    Embedding generation via Voyage AI cloud API.
    """

    def __init__(self, api_key: str, model: str = "voyageai/voyage-4-nano"):
        self.api_key = api_key
        self.model = model
        self.client = httpx.AsyncClient(
            timeout=60.0,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for text."""
        response = await self.client.post(
            "https://api.voyageai.com/v1/embeddings",
            json={"input": [text], "model": self.model},
        )
        response.raise_for_status()
        data = response.json()
        return data["data"][0]["embedding"]

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


class EmbeddingService:
    """
    Embedding generation using a local sentence-transformers service.
    Can fall back to Voyage AI cloud API.
    """

    def __init__(self):
        self.primary = HttpEmbeddings(
            base_url=settings.embedding_url,
            model=settings.embedding_model,
        )
        self.fallback = (
            VoyageEmbeddings(
                api_key=settings.voyage_api_key,
                model=settings.embedding_model,
            )
            if settings.voyage_api_key
            else None
        )
        self.dimension = settings.embedding_dimension
        self.circuit_breaker = CircuitBreaker()

    async def embed(
        self, text: str, use_fallback: bool = False
    ) -> list[float]:
        """
        Generate embedding for text.

        Args:
            text: Text to embed
            use_fallback: Force use of fallback provider

        Returns:
            Embedding vector
        """
        if use_fallback and self.fallback:
            embedding = await self.fallback.embed(text)
            return self._validate_dimension(embedding)

        async def primary_request():
            return await retry_async(lambda: self.primary.embed(text), retries=3, base_delay=1.0)

        try:
            embedding = await self.circuit_breaker.call(primary_request)
            return self._validate_dimension(embedding)
        except Exception as e:
            if self.fallback:
                logger.warning("Local embedding failed, using fallback: %s", e)
                embedding = await retry_async(lambda: self.fallback.embed(text), retries=2, base_delay=1.0)
                return self._validate_dimension(embedding)
            raise

    def _validate_dimension(self, embedding: list[float]) -> list[float]:
        """Validate embedding has the expected dimension."""
        if len(embedding) != self.dimension:
            raise ValueError(
                f"Embedding dimension mismatch: expected {self.dimension}, got {len(embedding)}"
            )
        return embedding

    async def embed_or_none(self, text: str) -> Optional[list[float]]:
        """
        Generate embedding, returning None instead of raising on failure.
        Useful for graceful degradation — callers can store content
        without an embedding and backfill later.
        """
        try:
            return await self.embed(text)
        except Exception as e:
            logger.warning("Embedding failed (graceful degradation): %s", e)
            return None

    async def embed_batch(
        self, texts: list[str], batch_size: int = 32
    ) -> list[list[float]]:
        """
        Batch embedding for efficiency.

        Args:
            texts: List of texts to embed
            batch_size: Number of texts to process in parallel

        Returns:
            List of embedding vectors
        """
        results = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            embeddings = await asyncio.gather(
                *[self.embed(text) for text in batch]
            )
            results.extend(embeddings)
        return results

    async def close(self):
        """Close HTTP clients."""
        await self.primary.close()
        if self.fallback:
            await self.fallback.close()


# Global instance
embedding_service = EmbeddingService()
