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
import re as _re
import struct
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from bson import Binary, ObjectId
from bson.binary import BinaryVectorDtype, VECTOR_SUBTYPE
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.core.bg import spawn_bg
from aria.config import settings
from aria.memory.capabilities import retrieval_capabilities
from aria.memory.embeddings import embedding_service

logger = logging.getLogger(__name__)

# Words that carry no selectivity in the mongod-native fallback scan. The
# fallback is a regex OR over content — a query like "what did I decide about
# the R9700" must not match every memory containing "the".
_FALLBACK_STOPWORDS = frozenset(
    """a an and are as at be but by for from has have how i if in into is it its
    me my of on or that the their there these they this to was were what when
    where which who why will with you your""".split()
)


class SearchBranchUnavailable(Exception):
    """A mongot-backed search branch could not answer.

    Raised instead of returning [] so `search()` can tell "mongot answered and
    nothing matched" apart from "mongot could not answer at all" — the second
    is what justifies falling back to the mongod-native scan, and conflating
    them is exactly how a dead mongot silently returned empty recall for days.
    """


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
    Encode an embedding as MongoDB's **native BSON vector** — Binary subtype 9
    (float32). This is the representation `$vectorSearch` / mongot expects; the
    old subtype-0 `struct.pack` format is why vector recall was brittle.
    See SHARED_SERVICES_DESIGN.md · S5.

    Args:
        embedding: List of float values

    Returns:
        BSON Binary (subtype 9) native float32 vector
    """
    return Binary.from_vector([float(x) for x in embedding], BinaryVectorDtype.FLOAT32)


def binary_to_embedding(binary_data: Binary) -> list[float]:
    """
    Decode a stored embedding to a list of floats.

    Handles both encodings so reads work during/after the S5 migration:
    - native BSON vector (subtype 9)  -> `as_vector().data`
    - legacy struct-packed float32 (subtype 0) -> `struct.unpack`

    Args:
        binary_data: BSON Binary object

    Returns:
        List of float values
    """
    if getattr(binary_data, "subtype", 0) == VECTOR_SUBTYPE:
        return list(binary_data.as_vector().data)
    # Legacy subtype-0: raw little-endian float32 (pre-S5 docs)
    num_floats = len(binary_data) // 4
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

    #: Hard ceiling on live entries. Each entry holds a full copy of a result
    #: list, so an unbounded cache is a memory leak with a TTL-shaped delay.
    MAX_ENTRIES = 256

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
        # Return a shallow copy so callers mutating the result don't corrupt
        # the cached list for other callers (cache aliasing bug).
        return list(results)

    def put(self, query: str, limit: int, filters: Optional[dict], results: list):
        key = self._make_key(query, limit, filters)
        # Store a shallow copy so a later caller mutation of the returned list
        # can't reach back into the cache entry.
        self._cache[key] = (time.monotonic(), list(results))
        # Sweep past the cap, then enforce it. The old code only swept when
        # already over 100 entries and stopped there, so a burst of >100
        # distinct queries inside one TTL window grew the cache without bound
        # for the length of the burst -- every entry a full result copy.
        if len(self._cache) > self.MAX_ENTRIES:
            now = time.monotonic()
            stale = [k for k, (ts, _) in self._cache.items() if now - ts > self._ttl]
            for k in stale:
                del self._cache[k]
        # Still over after the sweep: every entry is fresh, so evict oldest
        # first until the cap holds.
        while len(self._cache) > self.MAX_ENTRIES:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]

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

        Degrades by capability rather than failing (see memory/capabilities.py):

        | embeddings | mongot | what runs                                     |
        |------------|--------|-----------------------------------------------|
        | on         | on     | $vectorSearch + $search, fused by RRF (normal)|
        | off        | on     | $search (BM25) only — no query embedding      |
        | on/off     | off    | mongod-native fallback scan                   |

        The fallback also catches the *unplanned* case: if mongot is nominally
        enabled but every branch errors out, recall degrades to the scan
        instead of returning nothing. An empty result from a healthy mongot is
        still an empty result — only a branch that could not answer at all
        (SearchBranchUnavailable) triggers the fallback.

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

        # Build filter for every branch, including the fallback
        base_filter = {"status": "active"}
        if filters:
            base_filter.update(filters)

        if not retrieval_capabilities.search_enabled:
            # mongot is off on purpose — don't emit a stage it can't serve, and
            # don't spend an embedding on a vector we have nowhere to send.
            results = await self._fallback_search(query, base_filter, limit)
            self._cache.put(query, limit, filters, results)
            return results

        # Query embedding — skipped entirely when embeddings are off, which
        # turns the hybrid search into a BM25-only search rather than an error.
        query_embedding = None
        if retrieval_capabilities.embeddings_enabled:
            query_embedding = await embedding_service.embed(query)

        # Run the available branches in parallel
        branches = [self._lexical_search(query, base_filter, limit * 2)]
        if query_embedding is not None:
            branches.insert(
                0, self._vector_search(query_embedding, base_filter, limit * 2)
            )
        settled = await asyncio.gather(*branches, return_exceptions=True)

        if query_embedding is not None:
            vector_settled, lexical_settled = settled
        else:
            vector_settled, lexical_settled = [], settled[0]

        vector_results = self._branch_results(vector_settled, "vector")
        lexical_results = self._branch_results(lexical_settled, "lexical")
        vector_down = isinstance(vector_settled, SearchBranchUnavailable)
        lexical_down = isinstance(lexical_settled, SearchBranchUnavailable)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "Memory search completed: vector=%d lexical=%d in %.1fms",
            len(vector_results), len(lexical_results), elapsed_ms,
        )

        # Every branch that was supposed to answer failed → mongot is not
        # serving, whatever the switch says. Degrade rather than return [].
        if lexical_down and (vector_down or query_embedding is None):
            logger.error(
                "SEARCH UNAVAILABLE — every mongot branch failed; falling back to "
                "the mongod-native scan. If mongot is down on purpose, switch the "
                "'search' capability off so health stops paging."
            )
            results = await self._fallback_search(query, base_filter, limit)
            self._cache.put(query, limit, filters, results)
            return results

        # Combine with Reciprocal Rank Fusion
        fused = self._rrf_fusion(vector_results, lexical_results, k=60)

        results = self._apply_relevance_cliff(fused, max_results=limit)

        # Cache the results
        self._cache.put(query, limit, filters, results)

        return results

    @staticmethod
    def _branch_results(settled, name: str) -> list[tuple["Memory", float]]:
        """Unwrap one `asyncio.gather(..., return_exceptions=True)` slot."""
        if isinstance(settled, SearchBranchUnavailable):
            return []
        if isinstance(settled, BaseException):
            logger.error("Memory search %s branch raised unexpectedly: %s", name, settled)
            return []
        return settled

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
            # S5: surface this loudly — a failing vector branch silently degrades
            # recall to lexical-only. Post native-vector migration this should not
            # happen; if it does it's a real regression, not a warning to ignore.
            logger.error(
                "VECTOR SEARCH FAILED — recall degraded to lexical-only "
                "(check memory_vector_index / embedding encoding): %s", e,
            )
            raise SearchBranchUnavailable(str(e)) from e

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
        # Recall mitigation: the BM25 `$search` stage cannot share the arbitrary
        # MQL `filter` dict the way `$vectorSearch` does (mongot `$search`
        # filtering requires operator-form clauses, and `filter` here is a
        # dynamic MQL shape like {"status": "active", "private": {"$ne": True}}).
        # Without pushing the filter in, the post-`$match`/`$limit` would prune
        # an already-truncated result set and often return far fewer than
        # `limit`. So we fetch a much larger candidate set from `$search` before
        # filtering, so enough survive the `$match` to satisfy `limit`.
        search_limit = limit * 5
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
            {"$limit": search_limit},
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
            raise SearchBranchUnavailable(str(e)) from e

    async def _fallback_search(
        self, query: str, filter: dict, limit: int
    ) -> list[Memory]:
        """Recall with no mongot at all — a mongod-native scan.

        This is the whole point of being able to switch mongot off: recall gets
        worse, it does not stop. There is no BM25 and no vector here, so
        ranking is a deliberately crude token-overlap score, broken by
        importance and then recency:

            score = matched_tokens + importance   (recency breaks ties)

        Scanning is affordable because the candidate set is bounded by the
        regex `$or` and a hard `$limit`, and because this path only runs while
        a dependency is off. It is NOT a second-tier search engine — if these
        results start mattering, turn mongot back on.
        """
        tokens = [
            t
            for t in _re.split(r"\W+", query.lower())
            if len(t) > 2 and t not in _FALLBACK_STOPWORDS
        ][:8]

        mongo_query = dict(filter)
        # Bound the scan by recency. Without it a query that matches nothing
        # walks every memory in the collection, and that cost grows forever
        # while the switch stays off. Older memories stay reachable through
        # explicit search and the ontology; the ranking below already breaks
        # ties by recency, so this only trims what it was deprioritising.
        recency_days = int(getattr(settings, "memory_fallback_recency_days", 0) or 0)
        if recency_days > 0 and "created_at" not in mongo_query:
            cutoff = datetime.now(timezone.utc) - timedelta(days=recency_days)
            mongo_query["created_at"] = {"$gte": cutoff}
            logger.debug(
                "Fallback memory scan bounded to the last %d days -- older "
                "memories are not reachable this way while search is off.",
                recency_days,
            )
        if tokens:
            mongo_query["$or"] = [
                {"content": {"$regex": _re.escape(t), "$options": "i"}} for t in tokens
            ] + [{"categories": {"$in": tokens}}]

        # Wide candidate window so the ranking below has something to choose
        # from, capped so a stopword-only query can't scan the collection.
        scan_limit = max(limit * 20, 200)
        try:
            docs = (
                await self.db.memories.find(mongo_query)
                .sort("created_at", -1)
                .limit(scan_limit)
                .to_list(length=scan_limit)
            )
        except Exception as e:  # noqa: BLE001 — mongod itself is the last resort
            logger.error("Fallback memory scan failed (mongod unreachable?): %s", e)
            return []

        scored: list[tuple[float, datetime, Memory]] = []
        for doc in docs:
            content = (doc.get("content") or "").lower()
            hits = sum(1 for t in tokens if t in content) if tokens else 0
            score = hits + float(doc.get("importance") or 0.0)
            created = doc.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            scored.append((score, created, Memory.from_doc(doc)))

        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        results = [m for _, _, m in scored[:limit]]
        logger.info(
            "Memory recall served by the fallback scan (mode=%s): %d/%d candidates",
            retrieval_capabilities.retrieval_mode(),
            len(results),
            len(docs),
        )
        return results

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
        private: bool = False,
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
        # Generate embedding — gracefully degrade if the service is unavailable
        # OR switched off. Either way the memory is still written; the
        # embedding_pending flag below is the queue the backfill worker drains.
        embedding = await embedding_service.embed_or_none(content)

        dedup_filter = {"status": "active"}
        if private:
            dedup_filter["private"] = True
        else:
            dedup_filter["private"] = {"$ne": True}

        if embedding is not None and retrieval_capabilities.search_enabled:
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
                            "filter": dedup_filter,
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
        else:
            # No vector dedup available (embeddings and/or mongot off). Fall
            # back to exact-content dedup so a degraded window doesn't fill the
            # collection with literal duplicates — the emitters that write most
            # memories (shell extraction, machine scan, git changes) re-emit
            # identical text routinely, and the near-duplicate pass is the only
            # thing that has ever absorbed that.
            try:
                twin = await self.db.memories.find_one(
                    {**dedup_filter, "content": content}, {"_id": 1}
                )
                if twin:
                    logger.info(
                        "Skipping exact-duplicate memory (degraded dedup): %s", content[:80]
                    )
                    return str(twin["_id"])
            except Exception as e:
                logger.debug("Exact dedup check failed (non-fatal): %s", e)

        if embedding is not None:
            embedding_binary = embedding_to_binary(embedding)
        else:
            embedding_binary = None
            # Expected and quiet while the capability is off on purpose; a real
            # outage still deserves the warning.
            log = (
                logger.debug
                if not retrieval_capabilities.embeddings_enabled
                else logger.warning
            )
            log("Storing memory without embedding (embedding_pending): %s", content[:80])

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
            "private": private,
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

        # Ontology cross-link (§7 phase 5c) — fire-and-forget so the LLM tier
        # can never add latency to a memory write, and a cross-link failure can
        # never cost a memory. The cheap path-category tier is deterministic;
        # the LLM tier is gated behind ontology_extraction_enabled.
        if settings.ontology_enabled:
            try:
                from aria.ontology.crosslink import link_new_memory

                spawn_bg(
                    link_new_memory(
                        self.db, str(result.inserted_id), memory_doc["categories"]
                    ),
                    name="ontology:link_new_memory",
                )
            except Exception as e:  # noqa: BLE001 — never block memory creation
                logger.debug("ontology cross-link scheduling failed: %s", e)

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

        # If content changed, regenerate embedding — gracefully degrade if the
        # embedding service is unavailable (mirrors create_memory). On outage,
        # leave the existing embedding untouched rather than overwriting it with
        # null, and flag it as stale so it can be re-embedded later.
        if "content" in updates:
            embedding = await embedding_service.embed_or_none(updates["content"])
            if embedding is not None:
                updates["embedding"] = embedding_to_binary(embedding)
                updates["embedding_model"] = settings.embedding_model
                updates["embedding_pending"] = False
            else:
                log = (
                    logger.debug
                    if not retrieval_capabilities.embeddings_enabled
                    else logger.warning
                )
                log(
                    "Embedding unavailable; keeping existing embedding and "
                    "marking stale for memory %s (backfill will re-embed)",
                    memory_id,
                )
                updates["embedding_pending"] = True

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
