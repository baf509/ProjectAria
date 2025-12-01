"""
ARIA - Long-Term Memory

Phase: 2
Purpose: Semantic retrieval using hybrid BM25 + Vector search with RRF

Related Spec Sections:
- Section 3.3: Long-Term Memory Implementation
- Section 3.5: MongoDB Indexes for Memory
"""

import asyncio
import struct
from datetime import datetime
from typing import Optional
from bson import Binary, ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.memory.embeddings import embedding_service


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


class LongTermMemory:
    """
    Semantic retrieval using hybrid BM25 + Vector search.
    Uses Reciprocal Rank Fusion (RRF) to combine results.
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db

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

        # Combine with Reciprocal Rank Fusion
        fused = self._rrf_fusion(vector_results, lexical_results, k=60)

        return fused[:limit]

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
            print(f"Vector search error: {e}")
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
            print(f"Lexical search error: {e}")
            # Return empty results if lexical search fails
            return []

    def _rrf_fusion(
        self,
        vector_results: list[tuple[Memory, float]],
        lexical_results: list[tuple[Memory, float]],
        k: int = 60,
    ) -> list[Memory]:
        """
        Reciprocal Rank Fusion to combine result lists.
        RRF score = sum(1 / (k + rank)) for each list where doc appears.

        Args:
            vector_results: Results from vector search
            lexical_results: Results from lexical search
            k: RRF constant (typically 60)

        Returns:
            Fused and sorted list of memories
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

        return [r["memory"] for r in sorted_results]

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
        # Generate embedding
        embedding = await embedding_service.embed(content)

        # Convert embedding to BSON Binary for efficient storage
        embedding_binary = embedding_to_binary(embedding)

        # Create memory document
        memory_doc = {
            "content": content,
            "content_type": content_type,
            "embedding": embedding_binary,
            "embedding_model": settings.embedding_ollama_model,
            "source": source or {"type": "manual"},
            "status": "active",
            "importance": importance,
            "confidence": confidence,
            "verified": False,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "last_accessed_at": datetime.utcnow(),
            "access_count": 0,
            "categories": categories or [],
            "entities": [],
        }

        result = await self.db.memories.insert_one(memory_doc)
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
        updates["updated_at"] = datetime.utcnow()

        # If content changed, regenerate embedding
        if "content" in updates:
            embedding = await embedding_service.embed(updates["content"])
            updates["embedding"] = embedding_to_binary(embedding)
            updates["embedding_model"] = settings.embedding_ollama_model

        result = await self.db.memories.update_one(
            {"_id": ObjectId(memory_id)}, {"$set": updates}
        )

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
            {"$set": {"status": "deleted", "updated_at": datetime.utcnow()}},
        )

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
                "$set": {"last_accessed_at": datetime.utcnow()},
                "$inc": {"access_count": 1},
            },
        )


# Import settings for embedding model name
from aria.config import settings
