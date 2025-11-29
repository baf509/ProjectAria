"""
ARIA - Embedding Service

Phase: 2
Purpose: Generate embeddings using Qwen3-8b via Ollama (with Voyage AI fallback)

Related Spec Sections:
- Section 3.4: Embedding Service (Qwen3-8b)
"""

import asyncio
from typing import Optional

import httpx

from aria.config import settings


class OllamaEmbeddings:
    """
    Embedding generation via Ollama API.
    """

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(timeout=60.0)

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for text."""
        response = await self.client.post(
            f"{self.base_url}/api/embeddings",
            json={"model": self.model, "prompt": text},
        )
        response.raise_for_status()
        return response.json()["embedding"]

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


class VoyageEmbeddings:
    """
    Embedding generation via Voyage AI API.
    """

    def __init__(self, api_key: str, model: str = "voyage-3-large"):
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
    Local embedding generation using Qwen3-8b via Ollama.
    Can fall back to Voyage AI for quality-critical embeddings.
    """

    def __init__(self):
        self.primary = OllamaEmbeddings(
            base_url=settings.ollama_url,
            model=settings.embedding_ollama_model,
        )
        self.fallback = (
            VoyageEmbeddings(
                api_key=settings.voyage_api_key,
                model="voyage-3-large",
            )
            if settings.voyage_api_key
            else None
        )
        self.dimension = settings.embedding_dimension

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
            return await self.fallback.embed(text)

        try:
            return await self.primary.embed(text)
        except Exception as e:
            if self.fallback:
                print(f"Local embedding failed, using fallback: {e}")
                return await self.fallback.embed(text)
            raise

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
