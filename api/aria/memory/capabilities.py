"""
ARIA - Retrieval Capability Switches

Phase: 2 (Memory) / Ops
Purpose: Let mongot (search) and the embeddings model be turned OFF at runtime
         without taking ARIA down, and turned back on without losing anything.

Related Spec Sections:
- Section 3.3: Long-Term Memory Implementation
- Section 3.4: Embedding Service

WHY THIS EXISTS
===============
mongot and the embeddings container are two of the three `always_up` rows in the
non-LLM service registry, and both are pure *retrieval* infrastructure: nothing
in ARIA's control plane (shells, fleet, coding sessions, planning, the LLM
passthrough) needs either one. But before this module, stopping either meant:

- every `create_memory` still called the embedding service and paid a full
  retry + circuit-breaker cycle before degrading,
- every memory search issued `$vectorSearch`/`$search` against a mongot that
  wasn't there, logging a VECTOR SEARCH FAILED error per call,
- `/health/services` and the selfcheck worker paged Hermes's alert-triage cron
  every 10 minutes about a service that was off *on purpose*, and
- anything written while the embeddings service was down kept its
  `embedding_pending: true` flag forever — nothing ever came back for it.

So the switches here are the same idea as `model_servers`' "stopped on purpose":
a deliberately-disabled dependency is not a degraded one. Turning a capability
off changes ARIA's *behaviour* (skip the call, degrade the query, stay quiet in
health); it does not stop the container. Stopping the container is the service
registry's job, and the two are wired together in the capabilities route so one
call can do both.

THE INVARIANT THAT MAKES RE-ENABLING SAFE
=========================================
Everything written while embeddings are off is written with
`embedding_pending: true` (`long_term.create_memory`/`update_memory` already do
this — see the graceful-degradation branches there). Nothing is dropped, and the
flag is the queue. `memory/backfill.py` drains that queue: on a timer, and
immediately when embeddings are switched back on. Re-enabling is therefore not
a manual repair step — it is `set_embeddings(True)`, which kicks the worker.

State is persisted (fixed-`_id` doc, exactly like `Killswitch`) so a toggle
survives an `aria-api` restart. A capability that was deliberately off must not
quietly come back on at the next deploy — that would resurrect the alert storm
the operator turned off in the first place.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from aria.config import settings

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

COLLECTION = "capabilities"
DOC_ID = "retrieval"


class EmbeddingsDisabled(RuntimeError):
    """Raised by the embedding service when the capability is switched off.

    A subclass of RuntimeError on purpose: `/memories/search` and the memory
    API already map RuntimeError from the embedding path to a 503 with a
    reason, so a disabled capability degrades through the existing seam rather
    than surfacing as an opaque 500.
    """


class _Capability:
    """One switch, plus the provenance of the last flip."""

    def __init__(self, name: str, enabled: bool):
        self.name = name
        self.enabled = enabled
        self.reason: Optional[str] = None
        self.changed_at: Optional[datetime] = None
        self.changed_by: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "reason": self.reason,
            "changed_at": self.changed_at,
            "changed_by": self.changed_by,
        }

    def load(self, doc: dict) -> None:
        if "enabled" in doc:
            self.enabled = bool(doc["enabled"])
        self.reason = doc.get("reason")
        self.changed_at = doc.get("changed_at")
        self.changed_by = doc.get("changed_by")


class RetrievalCapabilities:
    """Runtime on/off switches for the two retrieval dependencies.

    `embeddings` — the sentence-transformers service on :8001. Off means: never
    call it. Writes still happen and are flagged `embedding_pending`; queries
    skip the vector branch.

    `search` — mongot, reached through mongod. Off means: never emit a
    `$vectorSearch` or `$search` stage. Queries fall back to a mongod-native
    scan (`long_term._fallback_search`), which is worse but real.

    They are independent because they fail independently: mongot can be down
    with embeddings fine (writes stay fully embedded, only recall degrades),
    and embeddings can be off with mongot fine (BM25 recall still works, and
    that is a genuinely useful degraded mode rather than a broken one).
    """

    def __init__(self):
        self._embeddings = _Capability("embeddings", bool(settings.embeddings_enabled))
        self._search = _Capability("search", bool(settings.search_enabled))
        self._db: Optional["AsyncIOMotorDatabase"] = None
        # Set by main.py so a flip can wake the backfill worker immediately
        # instead of waiting out its interval.
        self._on_embeddings_enabled = None

    # -- reads --------------------------------------------------------------

    @property
    def embeddings_enabled(self) -> bool:
        return self._embeddings.enabled

    @property
    def search_enabled(self) -> bool:
        return self._search.enabled

    @property
    def vector_search_enabled(self) -> bool:
        """`$vectorSearch` needs BOTH: a query vector and a mongot to serve it."""
        return self._embeddings.enabled and self._search.enabled

    def status(self) -> dict:
        return {
            "embeddings": self._embeddings.to_dict(),
            "search": self._search.to_dict(),
            "retrieval_mode": self.retrieval_mode(),
        }

    def retrieval_mode(self) -> str:
        """What `LongTermMemory.search` will actually do right now."""
        if self._search.enabled:
            return "hybrid" if self._embeddings.enabled else "lexical"
        return "fallback"

    # -- persistence --------------------------------------------------------

    def set_db(self, db: "AsyncIOMotorDatabase") -> None:
        self._db = db

    def set_backfill_trigger(self, callback) -> None:
        """Register a zero-arg callable invoked when embeddings are re-enabled."""
        self._on_embeddings_enabled = callback

    async def load_state(self, db: "AsyncIOMotorDatabase") -> None:
        """Load persisted switches on startup. Config defaults are the fallback."""
        self._db = db
        try:
            doc = await db[COLLECTION].find_one({"_id": DOC_ID})
        except Exception as exc:  # noqa: BLE001 — never block startup on this
            logger.warning("Could not load retrieval capabilities, using config defaults: %s", exc)
            return
        if not doc:
            return
        self._embeddings.load(doc.get("embeddings") or {})
        self._search.load(doc.get("search") or {})
        for cap in (self._embeddings, self._search):
            if not cap.enabled:
                logger.warning(
                    "Retrieval capability '%s' is DISABLED (persisted state): %s",
                    cap.name,
                    cap.reason or "no reason recorded",
                )

    async def _persist(self) -> None:
        if self._db is None:
            return
        try:
            await self._db[COLLECTION].update_one(
                {"_id": DOC_ID},
                {
                    "$set": {
                        "embeddings": self._embeddings.to_dict(),
                        "search": self._search.to_dict(),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001 — an unpersisted toggle still applies
            logger.warning("Could not persist retrieval capabilities: %s", exc)

    # -- writes -------------------------------------------------------------

    async def set_embeddings(
        self, enabled: bool, *, reason: str = "", changed_by: str = "api"
    ) -> dict:
        """Flip the embeddings switch. Re-enabling kicks the backfill worker."""
        was = self._embeddings.enabled
        self._apply(self._embeddings, enabled, reason, changed_by)
        await self._persist()

        if enabled and not was:
            logger.info("Embeddings ENABLED — draining the embedding_pending backlog")
            self._trigger_backfill()
        elif not enabled and was:
            logger.warning(
                "Embeddings DISABLED: %s — new memories will be stored "
                "embedding_pending and backfilled on re-enable",
                reason or "no reason given",
            )
        return self.status()

    async def set_search(
        self, enabled: bool, *, reason: str = "", changed_by: str = "api"
    ) -> dict:
        """Flip the mongot switch. Nothing to backfill — mongot indexes itself."""
        was = self._search.enabled
        self._apply(self._search, enabled, reason, changed_by)
        await self._persist()

        if enabled and not was:
            logger.info(
                "Search (mongot) ENABLED — memory recall back to %s", self.retrieval_mode()
            )
        elif not enabled and was:
            logger.warning(
                "Search (mongot) DISABLED: %s — recall degraded to the mongod-native "
                "fallback scan",
                reason or "no reason given",
            )
        return self.status()

    def _apply(self, cap: _Capability, enabled: bool, reason: str, changed_by: str) -> None:
        cap.enabled = bool(enabled)
        cap.reason = reason or None
        cap.changed_at = datetime.now(timezone.utc)
        cap.changed_by = changed_by or None

    def _trigger_backfill(self) -> None:
        if self._on_embeddings_enabled is None:
            return
        try:
            self._on_embeddings_enabled()
        except Exception as exc:  # noqa: BLE001 — the timer tick is the backstop
            logger.warning("Could not wake the embedding backfill worker: %s", exc)


# Global instance — imported directly, like `embedding_service` and the
# killswitch. Reads are a plain attribute lookup, so call sites can check the
# switch on every request without a round trip.
retrieval_capabilities = RetrievalCapabilities()
