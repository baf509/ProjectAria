"""
ARIA - Ontology store

Phase: Ontology Memory Map · Phases 1, 3
Purpose: CRUD + query over ontology_entities / ontology_relations, with the
S3 ownership rule enforced at the write boundary.

Related Spec Sections:
- ONTOLOGY_MEMORY_DESIGN.md §3 (data model + indexes), §5 (HTTP API surface)
- SHARED_SERVICES_DESIGN.md S3 (retired doc; convention lives in
  aria/shared/ownership.py)

The ownership rule is enforced HERE rather than in each caller, because there
will be several writers (projection, scan emitter, HTTP API, kg CLI) and only
one of them needs to get it wrong once to clobber prose Ben wrote by hand.
`upsert_entity(worker=True)` physically cannot write a protected field.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.memory.capabilities import retrieval_capabilities
from aria.memory.embeddings import embedding_service
from aria.memory.long_term import embedding_to_binary
from aria.ontology.models import (
    ENTITIES_COLLECTION,
    PROTECTED_FIELDS,
    RELATIONS_COLLECTION,
    WORKER_FIELDS,
    is_valid_predicate,
    is_valid_type,
    split_slug,
)
from aria.shared.ownership import merge_owned
from aria.shared.review import add_review_item

logger = logging.getLogger(__name__)

VECTOR_INDEX = "ontology_vector_index"
TEXT_INDEX = "ontology_text_index"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OntologyStore:
    """Read/write access to the graph."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.entities = db[ENTITIES_COLLECTION]
        self.relations = db[RELATIONS_COLLECTION]

    # -- Phase 1: indexes ---------------------------------------------------

    async def ensure_indexes(self) -> dict:
        """Idempotent index bootstrap (§3 Indexes). Safe to call every boot."""
        created = {"entities": [], "relations": []}
        try:
            await self.entities.create_index("type")
            await self.entities.create_index("status")
            await self.entities.create_index("tags")
            await self.entities.create_index([("type", 1), ("status", 1)])
            created["entities"] = ["type", "status", "tags", "type+status"]

            await self.relations.create_index("subject")
            await self.relations.create_index("object")
            await self.relations.create_index([("subject", 1), ("predicate", 1)])
            await self.relations.create_index([("object", 1), ("predicate", 1)])
            await self.relations.create_index("status")
            # One edge per (subject, predicate, object): re-running a projection
            # must refresh an edge, never stack duplicates of it.
            await self.relations.create_index(
                [("subject", 1), ("predicate", 1), ("object", 1)],
                unique=True,
                name="uniq_edge",
            )
            created["relations"] = [
                "subject", "object", "subject+predicate", "object+predicate",
                "status", "uniq_edge",
            ]

            # The cross-link index. `memories.entities[]` IS the memory<->graph
            # link (there are deliberately no bulk `mentions` edges — see
            # crosslink.py), so without this every "which memories mention X"
            # is a scan over 14,590 docs.
            await self.db.memories.create_index("entities")
            created["memories"] = ["entities"]
        except Exception as exc:  # noqa: BLE001 — index bootstrap is advisory
            logger.warning("ontology index bootstrap failed: %s", exc)
        return created

    # -- Entities -----------------------------------------------------------

    async def upsert_entity(
        self,
        slug: str,
        *,
        entity_type: Optional[str] = None,
        name: Optional[str] = None,
        attributes: Optional[dict] = None,
        summary: Optional[str] = None,
        aliases: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
        status: str = "active",
        confidence: float = 0.9,
        actor: str = "projection",
        worker: bool = True,
        embed: bool = False,
    ) -> dict:
        """Create or refresh an entity.

        `worker=True` (projections, the scan emitter) restricts writes to
        WORKER_FIELDS — a curated `summary`/`aliases`/`tags` survives untouched,
        and a contradiction is queued for review instead of overwriting.

        `worker=False` (a human via the API/CLI) may write the protected
        fields; that is the whole point of curation.
        """
        existing = await self.entities.find_one({"_id": slug}) or {}
        inferred_type, inferred_name = split_slug(slug)
        entity_type = entity_type or existing.get("type") or inferred_type
        if not is_valid_type(entity_type):
            raise ValueError(f"unknown entity type: {entity_type!r}")

        observed: dict[str, Any] = {
            "type": entity_type,
            "name": name or existing.get("name") or inferred_name,
            "status": status,
        }
        if attributes is not None:
            observed["attributes"] = attributes

        curated: dict[str, Any] = {}
        for field, value in (
            ("summary", summary),
            ("aliases", aliases),
            ("tags", tags),
        ):
            if value is not None:
                curated[field] = value

        now = _now()
        if worker:
            # `type` is added to the worker-owned set only for a brand-new doc;
            # re-typing an existing entity is a curation decision, not an
            # observation (a projection flipping machine->service would silently
            # relocate every edge that points at it).
            worker_fields = set(WORKER_FIELDS) | ({"type"} if not existing else set())
            set_update, conflicts = merge_owned(
                existing,
                {**observed, **curated},
                worker_fields=worker_fields,
                actor=actor,
            )
            for field in conflicts:
                await add_review_item(
                    self.db,
                    kind="conflict",
                    subject=slug,
                    detail=(
                        f"{actor} observed a different {field} for {slug} than the "
                        f"curated value; kept the curated one."
                    ),
                )
        else:
            set_update = {**observed, **curated}
            provenance = dict(existing.get("source") or {})
            for field in set_update:
                provenance[field] = {"actor": actor, "at": now}
            set_update["source"] = provenance
            set_update["last_verified_at"] = now

        set_update["updated_at"] = now
        set_update.setdefault("confidence", confidence)

        if embed:
            vector = await self._embed_entity(
                set_update.get("name") or existing.get("name") or "",
                curated.get("summary") or existing.get("summary") or "",
                curated.get("aliases") or existing.get("aliases") or [],
            )
            if vector is not None:
                set_update["embedding"] = vector
                set_update["embedding_model"] = settings.embedding_model

        await self.entities.update_one(
            {"_id": slug},
            {"$set": set_update, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        return await self.entities.find_one({"_id": slug})

    async def _embed_entity(
        self, name: str, summary: str, aliases: list[str]
    ) -> Optional[Any]:
        """1024-dim vector over name + aliases + summary, stored as BSON
        subtype 9 (S5). Degrades to None if the embedding service is down —
        a missing vector costs `kg search` recall, never correctness."""
        text = " ".join(filter(None, [name, " ".join(aliases or []), summary]))
        if not text.strip():
            return None
        vector = await embedding_service.embed_or_none(text)
        return embedding_to_binary(vector) if vector else None

    async def get_entity(self, slug: str) -> Optional[dict]:
        return await self.entities.find_one({"_id": slug})

    async def neighborhood(
        self, slug: str, include_mentions: bool = False
    ) -> Optional[dict]:
        """Entity + every incident STRUCTURAL edge, both directions (§5).

        `mentions` edges are excluded by default. Nothing writes them in bulk
        any more (see crosslink.py), but a single hand-authored batch would
        otherwise drown the structural edges that make this view useful — a
        real regression measured on `project:aria`, where 500 of 500 incoming
        edges were mentions and `runs_on machine:corsair-ai` was invisible.
        """
        entity = await self.get_entity(slug)
        if entity is None:
            return None
        base: dict = {"status": {"$ne": "removed"}}
        if not include_mentions:
            base["predicate"] = {"$ne": "mentions"}
        out = await self.relations.find({**base, "subject": slug}).to_list(length=500)
        inc = await self.relations.find({**base, "object": slug}).to_list(length=500)
        return {
            "entity": _clean(entity),
            "outgoing": [_clean_rel(r) for r in out],
            "incoming": [_clean_rel(r) for r in inc],
        }

    async def neighbors(
        self, slug: str, predicate: Optional[str] = None, direction: str = "both"
    ) -> list[dict]:
        """Edges from/to a node, optionally filtered by predicate (§5)."""
        clauses = []
        if direction in ("out", "both"):
            clauses.append({"subject": slug})
        if direction in ("in", "both"):
            clauses.append({"object": slug})
        query: dict = {"$or": clauses, "status": {"$ne": "removed"}}
        if predicate:
            query["predicate"] = predicate
        rows = await self.relations.find(query).to_list(length=1000)
        return [_clean_rel(r) for r in rows]

    async def map(self, entity_type: Optional[str] = None) -> list[dict]:
        """Typed overview — all machines, all services, ... (§5)."""
        query: dict = {"status": {"$ne": "removed"}}
        if entity_type:
            query["type"] = entity_type
        rows = await self.entities.find(query).sort("_id", 1).to_list(length=2000)
        return [_clean(r) for r in rows]

    async def search(
        self, query: str, entity_type: Optional[str] = None, limit: int = 10
    ) -> list[dict]:
        """Semantic search over entities, with a lexical fallback.

        Vector search needs the mongot index to exist; when it doesn't (fresh
        box, index still building) we fall back to a regex name/summary match
        rather than returning nothing, so `kg search` is useful on day one.
        """
        # `embed_or_none` already returns None when the embeddings capability is
        # off, so that half needs no guard here; the mongot half does — with
        # search switched off the $vectorSearch stage would error on every call
        # just to land in the lexical fallback below anyway.
        vector = (
            await embedding_service.embed_or_none(query)
            if retrieval_capabilities.search_enabled
            else None
        )
        if vector:
            try:
                pipeline: list[dict] = [
                    {
                        "$vectorSearch": {
                            "index": VECTOR_INDEX,
                            "path": "embedding",
                            "queryVector": vector,
                            "numCandidates": max(limit * 10, 100),
                            "limit": limit,
                        }
                    },
                    {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
                ]
                if entity_type:
                    pipeline.append({"$match": {"type": entity_type}})
                rows = await self.entities.aggregate(pipeline).to_list(length=limit)
                if rows:
                    return [_clean(r) for r in rows]
            except Exception as exc:  # noqa: BLE001 — index may not exist yet
                logger.debug("ontology vector search unavailable: %s", exc)

        return await self._lexical_search(query, entity_type, limit)

    async def _lexical_search(
        self, query: str, entity_type: Optional[str], limit: int
    ) -> list[dict]:
        import re as _re

        pattern = _re.escape(query.strip())
        if not pattern:
            return []
        mongo_query: dict = {
            "status": {"$ne": "removed"},
            "$or": [
                {"_id": {"$regex": pattern, "$options": "i"}},
                {"name": {"$regex": pattern, "$options": "i"}},
                {"summary": {"$regex": pattern, "$options": "i"}},
                {"aliases": {"$regex": pattern, "$options": "i"}},
                {"tags": {"$regex": pattern, "$options": "i"}},
            ],
        }
        if entity_type:
            mongo_query["type"] = entity_type
        rows = await self.entities.find(mongo_query).limit(limit).to_list(length=limit)
        return [_clean(r) for r in rows]

    async def mark_missing_stale(
        self, entity_type: str, seen_slugs: set[str], actor: str = "projection"
    ) -> list[str]:
        """Anything of this type we used to project but no longer observe goes
        `stale` — never deleted (S3: vanished things go stale, so a wrong
        projection is recoverable and history isn't destroyed)."""
        stale: list[str] = []
        query = {
            "type": entity_type,
            "status": "active",
            "source.attributes.actor": actor,
        }
        async for doc in self.entities.find(query):
            if doc["_id"] not in seen_slugs:
                await self.entities.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"status": "stale", "updated_at": _now()}},
                )
                stale.append(doc["_id"])
        return stale

    # -- Relations ----------------------------------------------------------

    async def upsert_relation(
        self,
        subject: str,
        predicate: str,
        obj: str,
        *,
        attributes: Optional[dict] = None,
        confidence: float = 0.9,
        actor: str = "projection",
        status: str = "active",
    ) -> dict:
        if not is_valid_predicate(predicate):
            raise ValueError(f"unknown predicate: {predicate!r}")
        now = _now()
        set_update = {
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "status": status,
            "confidence": confidence,
            "updated_at": now,
            "last_verified_at": now,
            "source": {"actor": actor, "at": now},
        }
        if attributes is not None:
            set_update["attributes"] = attributes
        await self.relations.update_one(
            {"subject": subject, "predicate": predicate, "object": obj},
            {"$set": set_update, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        return await self.relations.find_one(
            {"subject": subject, "predicate": predicate, "object": obj}
        )

    async def counts(self) -> dict:
        return {
            "entities": await self.entities.count_documents({}),
            "entities_active": await self.entities.count_documents({"status": "active"}),
            "relations": await self.relations.count_documents({}),
            "by_type": {
                t["_id"]: t["n"]
                async for t in self.entities.aggregate(
                    [{"$group": {"_id": "$type", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}]
                )
            },
        }


def _clean(doc: dict) -> dict:
    """Strip the embedding blob — it is binary, large, and never useful to a
    caller. Everything else passes through."""
    return {k: v for k, v in (doc or {}).items() if k != "embedding"}


def _clean_rel(doc: dict) -> dict:
    out = dict(doc or {})
    out.pop("_id", None)
    return out
