"""
ARIA - Embedding Backfill Worker

Phase: 2 (Memory) / Ops
Purpose: Re-embed everything that was written while the embeddings service was
         off or down, so switching the capability back on is self-healing.

Related Spec Sections:
- Section 3.4: Embedding Service
- memory/capabilities.py (the switches this worker completes)

THE CONTRACT
============
`long_term.create_memory` / `update_memory` never drop a write when they can't
embed: they store the memory and set `embedding_pending: true`. That flag IS the
queue. This worker is the consumer:

- on a timer (`embedding_backfill_interval_seconds`), so an embeddings outage
  that resolves itself needs no operator action at all, and
- immediately when the embeddings capability is switched back on
  (`RetrievalCapabilities.set_backfill_trigger` wires `kick()` in), so "turn it
  back on" and "catch up" are one action rather than two.

It also picks up two things the flag does not cover:
- memories with no `embedding` and no flag — pre-flag docs, and anything a
  future writer forgets to flag. `embedding: None` is the ground truth;
  the flag is only the fast index into it.
- ontology entities whose `_embed_entity` degraded to None, which otherwise
  cost `kg search` its vector branch permanently.

WHAT IT DELIBERATELY DOES NOT DO
================================
It does not run when the embeddings capability is off — that would defeat the
switch. It does not re-embed docs that already have a vector (no "refresh all"
mode; changing the model/dimension is a migration, not a backfill — see the
CLAUDE.md "Embedding Dimensions (DO NOT CHANGE)" gotcha). And it bounds itself
to `embedding_backfill_batch_size` docs per tick with a small concurrency cap,
because the embeddings service is CPU-only and a greedy drain would starve live
memory writes that are waiting on the same box.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.memory.capabilities import retrieval_capabilities
from aria.memory.embeddings import embedding_service
from aria.memory.long_term import embedding_to_binary
from aria.ontology.models import ENTITIES_COLLECTION

logger = logging.getLogger(__name__)

# A doc needs a vector if it is flagged pending OR simply has no embedding.
# Both clauses matter: the flag is what new writes set, the null is what
# pre-flag docs (and any writer that forgets the flag) look like.
PENDING_QUERY = {
    "status": "active",
    "$or": [{"embedding_pending": True}, {"embedding": None}],
}


class EmbeddingBackfillWorker:
    """Drains the embedding_pending backlog whenever embeddings are available."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        *,
        interval_seconds: Optional[int] = None,
        batch_size: Optional[int] = None,
        concurrency: Optional[int] = None,
    ):
        self.db = db
        self.interval = max(30, int(interval_seconds if interval_seconds is not None
                                    else settings.embedding_backfill_interval_seconds))
        self.batch_size = max(1, int(batch_size if batch_size is not None
                                     else settings.embedding_backfill_batch_size))
        self.concurrency = max(1, int(concurrency if concurrency is not None
                                      else settings.embedding_backfill_concurrency))
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._kick = asyncio.Event()
        self._running = asyncio.Lock()
        self.last_run_at: Optional[datetime] = None
        self.last_result: Optional[dict] = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="memory.embedding_backfill")
        logger.info(
            "embedding backfill worker started (every %ds, %d/batch)",
            self.interval,
            self.batch_size,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._kick.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    def kick(self) -> None:
        """Wake the worker now. Safe from a sync context (e.g. a route handler)."""
        self._kick.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()
            if self._stop.is_set():
                return
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 — a worker must not die
                logger.error("embedding backfill tick failed: %s", exc)

    # -- the work -----------------------------------------------------------

    async def pending_counts(self) -> dict:
        """How much is waiting. Cheap enough for the health/capabilities view."""
        try:
            memories = await self.db.memories.count_documents(PENDING_QUERY)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pending memory count failed: %s", exc)
            memories = -1
        try:
            entities = await self.db[ENTITIES_COLLECTION].count_documents(
                {"status": {"$ne": "removed"}, "embedding": None}
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("pending entity count failed: %s", exc)
            entities = -1
        return {"memories": memories, "entities": entities}

    async def run_once(self, *, batch_size: Optional[int] = None) -> dict:
        """One drain pass. Returns what it did — the route surfaces this verbatim.

        Serialised by a lock so a manual `POST .../backfill` racing the timer
        can't double-embed the same docs (wasted CPU, and the second write
        would land on a doc that no longer matches the query).
        """
        if self._running.locked():
            return {"skipped": "already running"}

        async with self._running:
            if not retrieval_capabilities.embeddings_enabled:
                # Not an error: the switch is off, so there is nothing to do
                # until it comes back on — at which point set_embeddings kicks us.
                return {"skipped": "embeddings disabled", "pending": await self.pending_counts()}

            limit = max(1, int(batch_size or self.batch_size))
            result = {
                "memories_embedded": 0,
                "memories_failed": 0,
                "entities_embedded": 0,
                "entities_failed": 0,
            }

            mem = await self._backfill_memories(limit)
            result["memories_embedded"] = mem[0]
            result["memories_failed"] = mem[1]

            # Only touch entities once memories are clean: memory recall is the
            # user-visible path, `kg search` already has a lexical fallback.
            if mem[1] == 0:
                ent = await self._backfill_entities(limit)
                result["entities_embedded"] = ent[0]
                result["entities_failed"] = ent[1]

            result["pending"] = await self.pending_counts()
            self.last_run_at = datetime.now(timezone.utc)
            self.last_result = result

            if result["memories_embedded"] or result["entities_embedded"]:
                logger.info(
                    "embedding backfill: %d memories, %d entities re-embedded "
                    "(%d memories still pending)",
                    result["memories_embedded"],
                    result["entities_embedded"],
                    result["pending"]["memories"],
                )
            return result

    async def _backfill_memories(self, limit: int) -> tuple[int, int]:
        docs = await self.db.memories.find(
            PENDING_QUERY, {"content": 1}
        ).limit(limit).to_list(length=limit)
        if not docs:
            return (0, 0)

        embedded = failed = 0
        sem = asyncio.Semaphore(self.concurrency)

        async def one(doc: dict) -> bool:
            content = (doc.get("content") or "").strip()
            if not content:
                # Nothing to embed, and leaving the flag set would make this doc
                # a permanent no-progress item at the head of every batch.
                await self.db.memories.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"embedding_pending": False, "embedding_skipped": "empty content"}},
                )
                return True
            async with sem:
                vector = await embedding_service.embed_or_none(content)
            if vector is None:
                return False
            await self.db.memories.update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "embedding": embedding_to_binary(vector),
                        "embedding_model": settings.embedding_model,
                        "embedding_pending": False,
                        "embedded_at": datetime.now(timezone.utc),
                    }
                },
            )
            return True

        for ok in await asyncio.gather(*[one(d) for d in docs], return_exceptions=True):
            if ok is True:
                embedded += 1
            else:
                failed += 1
                if isinstance(ok, BaseException):
                    logger.warning("backfill: memory embed raised: %s", ok)

        if failed:
            # The service is unavailable again (or the capability was flipped
            # mid-batch). Stop the pass here — the docs keep their flag and the
            # next tick retries them.
            logger.warning(
                "embedding backfill: %d/%d memories could not be embedded; "
                "leaving them pending for the next pass",
                failed,
                len(docs),
            )
        return (embedded, failed)

    async def _backfill_entities(self, limit: int) -> tuple[int, int]:
        """Ontology entities with no vector (store._embed_entity degraded)."""
        coll = self.db[ENTITIES_COLLECTION]
        docs = await coll.find(
            {"status": {"$ne": "removed"}, "embedding": None},
            {"name": 1, "summary": 1, "aliases": 1},
        ).limit(limit).to_list(length=limit)
        if not docs:
            return (0, 0)

        embedded = failed = 0
        for doc in docs:
            text = " ".join(
                filter(
                    None,
                    [
                        doc.get("name") or "",
                        " ".join(doc.get("aliases") or []),
                        doc.get("summary") or "",
                    ],
                )
            ).strip()
            if not text:
                continue
            vector = await embedding_service.embed_or_none(text)
            if vector is None:
                failed += 1
                break  # service is down again — stop, retry next tick
            await coll.update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "embedding": embedding_to_binary(vector),
                        "embedding_model": settings.embedding_model,
                    }
                },
            )
            embedded += 1
        return (embedded, failed)
