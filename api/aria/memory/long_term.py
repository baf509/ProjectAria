"""
ARIA - Long-Term Memory

Phase: 2
Purpose: Semantic retrieval using hybrid BM25 + Vector search with RRF

Related Spec Sections:
- Section 3.3: Long-Term Memory Implementation
- Section 3.5: MongoDB Indexes for Memory
"""

import asyncio
import hashlib
import logging
import struct
import time
from datetime import datetime, timezone
from typing import Optional
from bson import Binary, ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.memory.embeddings import embedding_service

logger = logging.getLogger(__name__)


class Memory:
    """Memory object."""

    def __init__(
        self,
        id: str,
        content: str,
        content_type: str,
        categories: list[str],
        importance: float,
        created_at: datetime,
        source: dict,
        confidence: Optional[float] = None,
        verified: bool = False,
    ):
        self.id = id
        self.content = content
        self.content_type = content_type
        self.categories = categories
        self.importance = importance
        self.created_at = created_at
        self.source = source
        self.confidence = confidence
        self.verified = verified

    @classmethod
    def from_doc(cls, doc: dict):
        """Create from MongoDB document."""
        return cls(
            id=str(doc["_id"]),
            content=doc["content"],
            content_type=doc["content_type"],
            categories=doc.get("categories", []),
            importance=doc.get("importance", 0.5),
            created_at=doc["created_at"],
            source=doc.get("source", {}),
            confidence=doc.get("confidence"),
            verified=doc.get("verified", False),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "content": self.content,
            "content_type": self.content_type,
            "categories": self.categories,
            "importance": self.importance,
            "created_at": self.created_at,
            "source": self.source,
            "confidence": self.confidence,
            "verified": self.verified,
        }


def embedding_to_binary(embedding: list[float]) -> Binary:
    """
    Convert embedding list to BSON Binary for efficient storage.

    Per MongoDB Atlas documentation, this provides compression and
    efficient storage of vector embeddings.

    Args:
        embedding: List of float values

    Returns:
        BSON Binary object with packed float data
    """
    # Pack the floats into binary format
    embedding_bytes = struct.pack(f'{len(embedding)}f', *embedding)
    # Return as BSON Binary with subtype 0 (generic binary)
    return Binary(embedding_bytes, subtype=0)


def binary_to_embedding(binary_data: Binary) -> list[float]:
    """
    Convert BSON Binary back to embedding list.

    Args:
        binary_data: BSON Binary object containing packed floats

    Returns:
        List of float values
    """
    # Calculate number of floats (each float is 4 bytes)
    num_floats = len(binary_data) // 4
    # Unpack the binary data back to floats
    return list(struct.unpack(f'{num_floats}f', binary_data))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class _SearchCache:
    """Simple TTL cache for memory search results."""

    def __init__(self, ttl_seconds: int = 10):
        self._cache: dict[str, tuple[float, list]] = {}
        self._ttl = ttl_seconds

    def _make_key(self, query: str, limit: int, filters: Optional[dict]) -> str:
        raw = f"{query}:{limit}:{sorted(filters.items()) if filters else ''}"
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, query: str, limit: int, filters: Optional[dict]) -> Optional[list]:
        key = self._make_key(query, limit, filters)
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, results = entry
        if time.monotonic() - ts > self._ttl:
            del self._cache[key]
            return None
        return results

    def put(self, query: str, limit: int, filters: Optional[dict], results: list):
        key = self._make_key(query, limit, filters)
        self._cache[key] = (time.monotonic(), results)
        # Evict stale entries periodically
        if len(self._cache) > 100:
            now = time.monotonic()
            stale = [k for k, (ts, _) in self._cache.items() if now - ts > self._ttl]
            for k in stale:
                del self._cache[k]

    def invalidate(self):
        """Clear all cached entries (call after memory mutation)."""
        self._cache.clear()


class LongTermMemory:
    """
    Semantic retrieval using hybrid BM25 + Vector search.
    Uses Reciprocal Rank Fusion (RRF) to combine results.
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self._cache = _SearchCache(ttl_seconds=settings.memory_search_cache_ttl_seconds)

    async def search(
        self, query: str, limit: int = 10, filters: dict = None
    ) -> list[Memory]:
        """
        Hybrid search: combines lexical (BM25) and semantic (vector) search.

        Args:
            query: Search query
            limit: Maximum number of results
            filters: Additional filters for search

        Returns:
            List of Memory objects sorted by relevance
        """
        # Check cache first
        cached = self._cache.get(query, limit, filters)
        if cached is not None:
            logger.debug("Memory search cache hit for query: %s", query[:50])
            return cached

        t0 = time.monotonic()

        # Generate embedding for query
        query_embedding = await embedding_service.embed(query)

        # Build filter for both searches
        base_filter = {"status": "active"}
        if filters:
            base_filter.update(filters)

        # Run both searches in parallel
        vector_results, lexical_results = await asyncio.gather(
            self._vector_search(query_embedding, base_filter, limit * 2),
            self._lexical_search(query, base_filter, limit * 2),
        )

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "Memory search completed: vector=%d lexical=%d in %.1fms",
            len(vector_results), len(lexical_results), elapsed_ms,
        )

        # Combine with Reciprocal Rank Fusion
        fused = self._rrf_fusion(vector_results, lexical_results, k=60)

        results = self._apply_relevance_cliff(fused, max_results=limit)

        # Cache the results
        self._cache.put(query, limit, filters, results)

        return results

    async def _vector_search(
        self, embedding: list[float], filter: dict, limit: int
    ) -> list[tuple[Memory, float]]:
        """
        MongoDB Atlas Vector Search.

        Args:
            embedding: Query embedding vector
            filter: Filter criteria
            limit: Maximum results

        Returns:
            List of (Memory, score) tuples
        """
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "memory_vector_index",
                    "path": "embedding",
                    "queryVector": embedding,
                    "numCandidates": limit * 10,
                    "limit": limit,
                    "filter": filter,
                }
            },
            {
                "$project": {
                    "content": 1,
                    "content_type": 1,
                    "categories": 1,
                    "importance": 1,
                    "created_at": 1,
                    "source": 1,
                    "confidence": 1,
                    "verified": 1,
                    "status": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]

        try:
            results = await self.db.memories.aggregate(pipeline).to_list(
                length=limit
            )
            return [(Memory.from_doc(r), r["score"]) for r in results]
        except Exception as e:
            logger.warning("Vector search error: %s", e)
            # Return empty results if vector search fails
            return []

    async def _lexical_search(
        self, query: str, filter: dict, limit: int
    ) -> list[tuple[Memory, float]]:
        """
        MongoDB Atlas Search with BM25 scoring.

        Args:
            query: Search query text
            filter: Filter criteria
            limit: Maximum results

        Returns:
            List of (Memory, score) tuples
        """
        pipeline = [
            {
                "$search": {
                    "index": "memory_text_index",
                    "text": {
                        "query": query,
                        "path": ["content", "categories"],
                        "fuzzy": {"maxEdits": 1},
                    },
                }
            },
            {"$match": filter},
            {"$limit": limit},
            {
                "$project": {
                    "content": 1,
                    "content_type": 1,
                    "categories": 1,
                    "importance": 1,
                    "created_at": 1,
                    "source": 1,
                    "confidence": 1,
                    "verified": 1,
                    "status": 1,
                    "score": {"$meta": "searchScore"},
                }
            },
        ]

        try:
            results = await self.db.memories.aggregate(pipeline).to_list(
                length=limit
            )
            return [(Memory.from_doc(r), r["score"]) for r in results]
        except Exception as e:
            logger.warning("Lexical search error: %s", e)
            # Return empty results if lexical search fails
            return []

    def _rrf_fusion(
        self,
        vector_results: list[tuple[Memory, float]],
        lexical_results: list[tuple[Memory, float]],
        k: int = 60,
    ) -> list[tuple[Memory, float]]:
        """
        Reciprocal Rank Fusion to combine result lists.
        RRF score = sum(1 / (k + rank)) for each list where doc appears.

        Args:
            vector_results: Results from vector search
            lexical_results: Results from lexical search
            k: RRF constant (typically 60)

        Returns:
            Fused and sorted list of (memory, rrf_score) tuples
        """
        scores = {}

        for rank, (memory, _) in enumerate(vector_results):
            doc_id = memory.id
            scores[doc_id] = scores.get(doc_id, {"memory": memory, "score": 0})
            scores[doc_id]["score"] += 1 / (k + rank + 1)

        for rank, (memory, _) in enumerate(lexical_results):
            doc_id = memory.id
            scores[doc_id] = scores.get(doc_id, {"memory": memory, "score": 0})
            scores[doc_id]["score"] += 1 / (k + rank + 1)

        # Sort by fused score
        sorted_results = sorted(
            scores.values(), key=lambda x: x["score"], reverse=True
        )

        return [(r["memory"], r["score"]) for r in sorted_results]

    def _apply_relevance_cliff(
        self,
        scored_results: list[tuple[Memory, float]],
        min_results: int = 1,
        max_results: int = 10,
    ) -> list[Memory]:
        """
        Detect a significant score drop between consecutive results and cut off there.

        Instead of a fixed threshold, this adapts to the query quality:
        strong matches with tight score clustering keep more results,
        weak queries with scattered scores get pruned aggressively.

        Args:
            scored_results: List of (Memory, score) sorted by score descending
            min_results: Always return at least this many
            max_results: Never return more than this many

        Returns:
            Pruned list of Memory objects
        """
        if len(scored_results) <= min_results:
            return [m for m, _ in scored_results]

        capped = scored_results[:max_results]
        if len(capped) < 2:
            return [m for m, _ in capped]

        scores = [s for _, s in capped]

        # Compute drops between consecutive scores
        drops = [scores[i] - scores[i + 1] for i in range(len(scores) - 1)]

        if not drops:
            return [m for m, _ in capped]

        mean_drop = sum(drops) / len(drops)
        if len(drops) > 1:
            variance = sum((d - mean_drop) ** 2 for d in drops) / len(drops)
            stddev = variance ** 0.5
        else:
            stddev = 0.0

        # Find the first drop exceeding mean + 1.5 * stddev
        threshold = mean_drop + 1.5 * stddev
        cutoff = len(capped)

        if stddev > 0:
            for i, drop in enumerate(drops):
                if drop > threshold and (i + 1) >= min_results:
                    cutoff = i + 1
                    logger.debug(
                        "Relevance cliff at position %d: drop=%.4f threshold=%.4f",
                        cutoff, drop, threshold,
                    )
                    break

        return [m for m, _ in capped[:cutoff]]

    async def create_memory(
        self,
        content: str,
        content_type: str,
        categories: list[str] = None,
        importance: float = 0.5,
        confidence: float = None,
        source: dict = None,
    ) -> str:
        """
        Create a new memory with embedding.
        Checks for near-duplicate content before inserting.

        Args:
            content: Memory content
            content_type: Type of memory (fact, preference, event, skill, document)
            categories: Categories/tags
            importance: Importance score 0.0-1.0
            confidence: Confidence score 0.0-1.0
            source: Source information

        Returns:
            Created memory ID
        """
        # Generate embedding — gracefully degrade if service is unavailable
        embedding = await embedding_service.embed_or_none(content)

        if embedding is not None:
            # Deduplication: check for near-duplicates via vector search
            threshold = settings.memory_dedup_similarity_threshold
            try:
                pipeline = [
                    {
                        "$vectorSearch": {
                            "index": "memory_vector_index",
                            "path": "embedding",
                            "queryVector": embedding,
                            "numCandidates": 20,
                            "limit": 1,
                            "filter": {"status": "active"},
                        }
                    },
                    {
                        "$project": {
                            "content": 1,
                            "score": {"$meta": "vectorSearchScore"},
                        }
                    },
                ]
                existing = await self.db.memories.aggregate(pipeline).to_list(length=1)
                if existing and existing[0].get("score", 0) >= threshold:
                    logger.info(
                        "Skipping duplicate memory (similarity=%.3f): %s",
                        existing[0]["score"],
                        content[:80],
                    )
                    return str(existing[0]["_id"])
            except Exception as e:
                # Dedup is best-effort — don't block memory creation
                logger.debug("Dedup check failed (non-fatal): %s", e)

            embedding_binary = embedding_to_binary(embedding)
        else:
            embedding_binary = None
            logger.warning("Storing memory without embedding (embedding_pending): %s", content[:80])

        # Create memory document
        memory_doc = {
            "content": content,
            "content_type": content_type,
            "embedding": embedding_binary,
            "embedding_model": settings.embedding_model if embedding_binary else None,
            "embedding_pending": embedding_binary is None,
            "source": source or {"type": "manual"},
            "status": "active",
            "importance": importance,
            "confidence": confidence,
            "verified": False,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "last_accessed_at": datetime.now(timezone.utc),
            "access_count": 0,
            "categories": categories or [],
            "entities": [],
        }

        result = await self.db.memories.insert_one(memory_doc)

        # Invalidate search cache after mutation
        self._cache.invalidate()

        return str(result.inserted_id)

    async def update_memory(
        self, memory_id: str, updates: dict
    ) -> bool:
        """
        Update a memory.

        Args:
            memory_id: Memory ID
            updates: Fields to update

        Returns:
            True if updated
        """
        updates["updated_at"] = datetime.now(timezone.utc)

        # If content changed, regenerate embedding
        if "content" in updates:
            embedding = await embedding_service.embed(updates["content"])
            updates["embedding"] = embedding_to_binary(embedding)
            updates["embedding_model"] = settings.embedding_model

        result = await self.db.memories.update_one(
            {"_id": ObjectId(memory_id)}, {"$set": updates}
        )

        # Invalidate search cache after mutation
        self._cache.invalidate()

        return result.modified_count > 0

    async def delete_memory(self, memory_id: str) -> bool:
        """
        Soft delete a memory (set status to deleted).

        Args:
            memory_id: Memory ID

        Returns:
            True if deleted
        """
        result = await self.db.memories.update_one(
            {"_id": ObjectId(memory_id)},
            {"$set": {"status": "deleted", "updated_at": datetime.now(timezone.utc)}},
        )

        # Invalidate search cache after mutation
        self._cache.invalidate()

        return result.modified_count > 0

    async def increment_access(self, memory_id: str):
        """
        Increment access count for a memory.

        Args:
            memory_id: Memory ID
        """
        await self.db.memories.update_one(
            {"_id": ObjectId(memory_id)},
            {
                "$set": {"last_accessed_at": datetime.now(timezone.utc)},
                "$inc": {"access_count": 1},
            },
        )

    async def batch_increment_access(self, memory_ids: list[str]):
        """
        Increment access count for multiple memories in a single operation.

        Args:
            memory_ids: List of memory IDs
        """
        if not memory_ids:
            return
        try:
            object_ids = [ObjectId(mid) for mid in memory_ids]
            await self.db.memories.update_many(
                {"_id": {"$in": object_ids}},
                {
                    "$set": {"last_accessed_at": datetime.now(timezone.utc)},
                    "$inc": {"access_count": 1},
                },
            )
        except Exception as e:
            logger.warning("Failed to batch increment memory access: %s", e)
