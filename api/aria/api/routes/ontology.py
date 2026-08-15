"""
ARIA - Ontology Routes

Phase: Ontology Memory Map · Phase 3
Purpose: Query and curate the knowledge graph of Ben's world.

Related Spec Sections:
- ONTOLOGY_MEMORY_DESIGN.md §5 (HTTP API), §7 (memory<->graph cross-link)

Writes go through the global X-API-Key middleware (S4), same as every other
mutating route. A write from here is CURATION — it may set the protected
fields (`summary`, `aliases`, `tags`) that a projection is forbidden to touch.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_db
from aria.ontology.crosslink import (
    backfill_path_categories,
    extract_entities_for_memory,
    memories_mentioning,
    run_targeted_backfill,
)
from aria.ontology.models import ENTITY_TYPES, PREDICATES
from aria.ontology.projection import run_projection
from aria.ontology.store import OntologyStore

router = APIRouter(prefix="/ontology", tags=["ontology"])


def _store(db: AsyncIOMotorDatabase) -> OntologyStore:
    return OntologyStore(db)


class EntityUpsert(BaseModel):
    slug: Optional[str] = Field(default=None, description="type:name; derived if omitted")
    type: Optional[str] = Field(default=None, description=f"One of {list(ENTITY_TYPES)}")
    name: Optional[str] = None
    summary: Optional[str] = None
    aliases: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    attributes: Optional[dict] = None
    status: str = "active"
    confidence: float = 0.9


class RelationUpsert(BaseModel):
    subject: str
    predicate: str = Field(description=f"One of {list(PREDICATES)}")
    object: str
    attributes: Optional[dict] = None
    confidence: float = 0.9


class SearchRequest(BaseModel):
    query: str
    type: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=100)


class BackfillRequest(BaseModel):
    dry_run: bool = Field(
        default=True,
        description="Report what would change without writing. Defaults to TRUE "
        "because this rewrites entities[] across the memory store.",
    )
    limit: Optional[int] = Field(default=None, ge=1)


@router.get("/vocabulary")
async def vocabulary():
    """The closed sets a client should validate against before writing."""
    return {"entity_types": list(ENTITY_TYPES), "predicates": list(PREDICATES)}


@router.get("/stats")
async def stats(db: AsyncIOMotorDatabase = Depends(get_db)):
    return await _store(db).counts()


@router.get("/map")
async def entity_map(
    type: Optional[str] = Query(default=None, description="Filter by entity type"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Typed overview — all machines, all services, ... (§5)."""
    if type and type not in ENTITY_TYPES:
        raise HTTPException(status_code=400, detail=f"unknown type '{type}'")
    return {"entities": await _store(db).map(type)}


@router.get("/neighbors")
async def neighbors(
    slug: str,
    predicate: Optional[str] = None,
    direction: str = Query(default="both", pattern="^(in|out|both)$"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    if predicate and predicate not in PREDICATES:
        raise HTTPException(status_code=400, detail=f"unknown predicate '{predicate}'")
    return {"relations": await _store(db).neighbors(slug, predicate, direction)}


@router.post("/search")
async def search(body: SearchRequest, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Semantic search over entities, with lexical fallback (§5)."""
    if body.type and body.type not in ENTITY_TYPES:
        raise HTTPException(status_code=400, detail=f"unknown type '{body.type}'")
    return {"results": await _store(db).search(body.query, body.type, body.limit)}


@router.post("/project")
async def project(
    embed: bool = Query(default=True),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Re-derive the projected half of the graph (§4a). Idempotent."""
    return await run_projection(db, embed=embed)


@router.post("/crosslink/path-categories")
async def crosslink_path_categories(
    body: BackfillRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """§7a — deterministic, LLM-free `entities[]` seeding from path categories.

    Defaults to a dry run: it touches thousands of memory docs, and a
    most-specific-root bug would mis-attribute all of them at once.
    """
    return await backfill_path_categories(db, dry_run=body.dry_run, limit=body.limit)


@router.post("/crosslink/backfill")
async def crosslink_backfill(
    body: BackfillRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """§7 piece 2 — LLM entity extraction over the ~919 curated memories.

    Deliberately scoped to the curated sources. The 13,671 machine-generated
    memories (shell_extraction + claude_session_digest) are excluded by
    decision, not by oversight — see §8, Phase 5e (closed 2026-08-07).
    """
    return await run_targeted_backfill(db, dry_run=body.dry_run, limit=body.limit)


@router.post("/entity")
async def upsert_entity(body: EntityUpsert, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Curate an entity. Unlike a projection, this MAY write the protected
    fields — that is what curation is."""
    from aria.ontology.models import entity_slug

    slug = body.slug
    if not slug:
        if not (body.type and body.name):
            raise HTTPException(
                status_code=400, detail="provide either slug, or both type and name"
            )
        slug = entity_slug(body.type, body.name)
    try:
        doc = await _store(db).upsert_entity(
            slug,
            entity_type=body.type,
            name=body.name,
            summary=body.summary,
            aliases=body.aliases,
            tags=body.tags,
            attributes=body.attributes,
            status=body.status,
            confidence=body.confidence,
            actor="human",
            worker=False,
            embed=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {k: v for k, v in doc.items() if k != "embedding"}


@router.post("/relation")
async def upsert_relation(
    body: RelationUpsert, db: AsyncIOMotorDatabase = Depends(get_db)
):
    store = _store(db)
    for slug in (body.subject, body.object):
        if await store.get_entity(slug) is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown entity '{slug}' — create it before linking to it",
            )
    try:
        doc = await store.upsert_relation(
            body.subject,
            body.predicate,
            body.object,
            attributes=body.attributes,
            confidence=body.confidence,
            actor="human",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    doc.pop("_id", None)
    return doc


@router.post("/memory/{memory_id}/extract")
async def extract_for_memory(
    memory_id: str, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """Extract entities for one memory (§7). Used by the forward-only path and
    handy for spot-checking extraction quality."""
    result = await extract_entities_for_memory(db, memory_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"unknown memory {memory_id}")
    return result


@router.get("/entity/{slug}/memories")
async def entity_memories(
    slug: str,
    limit: int = Query(default=25, ge=1, le=200),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Which memories refer to this entity — the graph -> memory direction.

    Reads `memories.entities[]` rather than an edge collection. There are
    deliberately no bulk `mentions` edges: they duplicated this array, grew
    ~670/day, and buried every structural edge in the neighborhood view.
    """
    return {"slug": slug, "memories": await memories_mentioning(db, slug, limit)}


# NOTE: registered last so it cannot shadow /map, /search, /neighbors, /stats,
# /vocabulary, /project, /entity, /relation. FastAPI matches in declaration
# order, and `/{slug}` would otherwise swallow every one of them.
@router.get("/{slug:path}")
async def get_entity(slug: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Entity + its full neighborhood (§5)."""
    hood = await _store(db).neighborhood(slug)
    if hood is None:
        raise HTTPException(status_code=404, detail=f"unknown entity '{slug}'")
    return hood
