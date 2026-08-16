"""
ARIA - Memory <-> graph cross-link

Phase: Ontology Memory Map · Phases 5b, 5c, 5d
Purpose: Fill `memories.entities[]` with entity slugs and emit `mentions`
edges, connecting the flat memory store into the graph.

Related Spec Sections:
- vault/ProjectAria/Design/ARCHITECTURE.md (Ontology Memory Map)
  (phase split: forward-only extraction + a resumable curated backfill)

SCOPE, AND WHY IT IS NOT "ONE PASS OVER aria.memories"
======================================================
The original plan said: one extraction pass over ~1,186 memories. The store is
now 14,590 and grows ~670/day, on a host that has had no cloud fallback since
2026-07-26. A whole-store LLM pass is hours of serialized local GPU time and
would be stale before it finished. So this module implements three pieces of
decreasing priority:

  5b  backfill_path_categories()  - deterministic, LLM-FREE, thousands of docs
                                    at zero inference cost. Run this first.
  5c  extract_entities_for_memory() - forward-only, one memory at a time.
  5d  run_targeted_backfill()     - the ~919 CURATED memories only.

  5e  the 13,671 machine-generated memories (shell_extraction +
      claude_session_digest) get NO LLM pass. Closed by decision 2026-08-07,
      not deferred. BULK_SOURCE_TYPES below is what enforces it.

WHY NO `mentions` EDGES (deviation from §3, decided 2026-08-07)
==============================================================
The design lists `mentions` as a memory->entity predicate, and the first
implementation wrote one edge per (memory, entity) pair. Measured on the real
store, that was wrong in three ways:

  1. Redundant. `memories.entities[]` already holds the link and is the hook
     §7 names. The edge stores the same fact twice.
  2. Unbounded. ~3,700 edges from the first backfill alone, growing ~670/day
     forever — a quarter-million rows a year that answer no question the
     `entities[]` array cannot.
  3. It BROKE the graph's primary query. `project:aria` came back with 500 of
     500 incoming edges being `mentions`, burying the structural
     `runs_on machine:corsair-ai` edge that makes the graph worth having.

So the cross-link is `entities[]` plus an index on it, and "which memories
mention X" is answered by querying memories (the authoritative side) via
`memories_mentioning()`. The `mentions` predicate stays in the vocabulary for
a human to hand-author a deliberate edge; nothing writes it in bulk.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.ontology.models import project_entity_slug, project_roots, split_slug
from aria.ontology.store import OntologyStore

logger = logging.getLogger(__name__)

# Sources worth spending inference on: hand-written, curated, or structural.
CURATED_SOURCE_TYPES: tuple[str, ...] = (
    "claude_code",
    "claude_export",
    "machine_scan",
    "dream_consolidation",
    "conversation",
    "shell_curation",
    "memory_api",
)
# Machine-generated session residue. Excluded from LLM extraction BY DECISION
# (§8, phase 5e). They still get the free path-category treatment in 5b.
BULK_SOURCE_TYPES: tuple[str, ...] = (
    "shell_extraction",
    "claude_session_digest",
)

HOME = os.path.expanduser("~")


def _norm_path(p: Optional[str]) -> Optional[str]:
    """Normalise a path-ish string, expanding `~` to the real home.

    Memory categories are stored as `~/Development/ProjectAria` while projects
    carry absolute paths, so without this expansion nothing would ever match.
    """
    if not p:
        return None
    p = p.strip()
    if p.startswith("~"):
        p = HOME + p[1:]
    return p.rstrip("/") or "/"


def looks_like_path(category: str) -> bool:
    """True for the path-shaped categories (`~/Development/ProjectAria`).

    Topical categories (`infrastructure`, `llm`, `aria`) are tags, not
    entities, and are deliberately NOT forced into the graph (§7a).
    """
    return bool(category) and (category.startswith("~/") or category.startswith("/"))


class PathProjectIndex:
    """Most-specific-root path -> project entity slug.

    Mirrors C4's `PathIndex` (api/routes/digest.py) on purpose. Plain prefix
    matching is a trap here: a harvested row for `~/Development` itself would
    swallow every child project, so `~/Development/ProjectAria` would be
    attributed to `project:development`. The longest matching root wins.
    """

    def __init__(self, entries: list[tuple[str, str]]):
        # Longest root first — the whole point. The slug is a SECONDARY sort
        # key so ties are deterministic: two projects genuinely do claim
        # ~/Development/ProjectAria ("ARIA" and "ProjectAria"), and without a
        # stable tiebreak the winner would depend on Mongo's iteration order,
        # silently reassigning thousands of memories between runs.
        self._entries = sorted(entries, key=lambda e: (-len(e[0]), e[1]))

    @classmethod
    async def build(cls, db: AsyncIOMotorDatabase) -> "PathProjectIndex":
        entries: list[tuple[str, str]] = []
        async for doc in db.projects.find({}):
            if not (doc.get("slug") or doc.get("name")):
                continue
            slug = project_entity_slug(doc)
            for raw in project_roots(doc):
                root = _norm_path(raw)
                if root:
                    entries.append((root, slug))
        return cls(entries)

    def owner(self, candidate: Optional[str]) -> Optional[str]:
        c = _norm_path(candidate)
        if not c:
            return None
        for root, slug in self._entries:
            if c == root or c.startswith(root + "/"):
                return slug
        return None

    def __len__(self) -> int:
        return len(self._entries)


async def backfill_path_categories(
    db: AsyncIOMotorDatabase,
    *,
    dry_run: bool = True,
    limit: Optional[int] = None,
) -> dict:
    """§7a — seed `entities[]` from path-shaped categories. No LLM.

    Applies to EVERY memory including the bulk sources: it costs nothing per
    doc, so the 5e exclusion (which is about inference cost) does not apply.

    Idempotent: entity slugs are added with `$addToSet`, so re-running cannot
    duplicate them.
    """
    index = await PathProjectIndex.build(db)
    store = OntologyStore(db)

    known: set[str] = set()
    async for doc in db.ontology_entities.find({"type": "project"}, {"_id": 1}):
        known.add(doc["_id"])

    scanned = matched = updated = 0
    unmatched_paths: dict[str, int] = {}
    per_entity: dict[str, int] = {}

    cursor = db.memories.find(
        {"categories": {"$regex": "^[~/]"}, "status": {"$ne": "pruned"}},
        {"categories": 1, "entities": 1},
    )
    if limit:
        cursor = cursor.limit(limit)

    async for mem in cursor:
        scanned += 1
        slugs: set[str] = set()
        for cat in mem.get("categories") or []:
            if not looks_like_path(cat):
                continue
            slug = index.owner(cat)
            if slug is None:
                unmatched_paths[cat] = unmatched_paths.get(cat, 0) + 1
                continue
            # Never link to an entity that isn't in the graph — a dangling
            # slug in entities[] is worse than no link at all.
            if slug in known:
                slugs.add(slug)

        if not slugs:
            continue
        matched += 1
        existing = set(mem.get("entities") or [])
        new = slugs - existing
        if not new:
            continue

        for slug in new:
            per_entity[slug] = per_entity.get(slug, 0) + 1

        if not dry_run:
            # `entities[]` IS the cross-link — see WHY NO `mentions` EDGES in
            # the module docstring. Writing a parallel edge per memory would
            # add ~670 rows/day that duplicate this array and bury every
            # structural edge in the graph.
            await db.memories.update_one(
                {"_id": mem["_id"]},
                {"$addToSet": {"entities": {"$each": sorted(new)}}},
            )
        updated += 1

    return {
        "dry_run": dry_run,
        "project_roots_indexed": len(index),
        "memories_scanned": scanned,
        "memories_matched": matched,
        "memories_updated": updated,
        "by_entity": dict(sorted(per_entity.items(), key=lambda kv: -kv[1])[:20]),
        "unmatched_paths": dict(
            sorted(unmatched_paths.items(), key=lambda kv: -kv[1])[:20]
        ),
    }


# --------------------------------------------------------------------------
# 5c / 5d — LLM extraction
# --------------------------------------------------------------------------

_EXTRACT_PROMPT = """You are labelling one memory with the entities it refers to.

Choose ONLY from this list of known entities. Do not invent new ones.
{catalog}

Memory:
\"\"\"{content}\"\"\"

Return STRICT JSON: {{"entities": ["type:name", ...]}}
Include an entity only if the memory genuinely refers to that specific thing.
An empty list is the correct answer for a memory about none of them.
Return at most 6."""


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_object(raw: str) -> dict:
    """Pull a JSON object out of a model response.

    Deliberately tolerant, because the model on the other end is NOT fixed:
    `llamacpp` resolves to ARIA's /llm/v1 passthrough, which follows whichever
    server is resident. That is currently DS4, which emits
    `<think>...</think>` ahead of its answer — a strict `json.loads` returned
    zero entities for every memory. Tomorrow it might be a model that fences
    its output or adds a preamble.

    Order matters: strip reasoning blocks, strip markdown fences, then take the
    outermost {...} span.
    """
    text = _THINK_RE.sub("", raw or "").strip()
    if text.startswith("```"):
        text = "\n".join(
            l for l in text.splitlines() if not l.strip().startswith("```")
        ).strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    match = _OBJECT_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object in response: {text[:120]!r}")
    return json.loads(match.group(0))


async def _entity_catalog(db: AsyncIOMotorDatabase, limit: int = 400) -> list[str]:
    """Slugs the extractor is allowed to choose from.

    A closed vocabulary is deliberate: free-form extraction would mint
    near-duplicate entities ("mongo", "mongodb", "shared-mongod") and the graph
    would need a dedup pass nobody has written.

    `person` is excluded: there is exactly one person in this graph, so the
    label carries no information — and every memory quoting a path under
    /home/ben/ was getting tagged with it.
    """
    slugs: list[str] = []
    async for doc in db.ontology_entities.find(
        {"status": "active", "type": {"$ne": "person"}}, {"_id": 1}
    ).limit(limit):
        slugs.append(doc["_id"])
    return slugs


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}


def verify_slug(slug: str, content: str, aliases: Optional[list[str]] = None) -> bool:
    """Deterministic evidence check for one proposed entity.

    Demotes the LLM from decider to candidate-generator: it may propose, but
    the memory text must actually contain the entity's name. Measured on the
    first 8 extractions (2026-08-07), the model produced these false positives
    that this gate rejects:

      - 'ling-3.0-flash-q5km.service stopped'  -> service:ling-3.0-flash-MXFP4
        (the wrong quant of the right family — the most dangerous kind of
        wrong, since it looks right)
      - "container 'hermes-83c95e06' started"  -> service:hermes-webui
      - a memory about the model-server registry -> service:samba
      - a memory about war-audio-game           -> machine:red

    It costs some true positives (a memory saying "aria.memories" will not
    match `datastore:aria-db`), which is the right trade: a wrong edge in a
    knowledge graph is worse than a missing one, because it gets believed.
    """
    haystack = _tokens(content)
    for candidate in [split_slug(slug)[1], *(aliases or [])]:
        needed = _tokens(candidate)
        if needed and needed <= haystack:
            return True
    return False


async def extract_entities_for_memory(
    db: AsyncIOMotorDatabase,
    memory_id: str,
    *,
    catalog: Optional[list[str]] = None,
    dry_run: bool = False,
) -> Optional[dict]:
    """§7 piece 1/3 — label ONE memory. Returns None if the memory is absent.

    Degrades to `{"entities": []}` on any LLM or parse failure: an unlabelled
    memory is the status quo, whereas a raised exception would break whatever
    write path called this.
    """
    try:
        oid = ObjectId(memory_id)
    except Exception:  # noqa: BLE001 — non-ObjectId ids are simply unknown here
        return None
    mem = await db.memories.find_one({"_id": oid}, {"content": 1, "entities": 1})
    if mem is None:
        return None

    catalog = catalog if catalog is not None else await _entity_catalog(db)
    if not catalog:
        return {"memory_id": memory_id, "entities": [], "detail": "empty catalog"}

    content = (mem.get("content") or "")[:2000]
    prompt = _EXTRACT_PROMPT.format(
        catalog="\n".join(f"- {s}" for s in catalog), content=content
    )

    slugs: list[str] = []
    try:
        from aria.llm.base import Message
        from aria.llm.manager import llm_manager

        adapter = llm_manager.get_adapter(
            settings.ontology_extraction_backend or "llamacpp",
            settings.ontology_extraction_model or "",
        )
        response, _, _ = await adapter.complete(
            messages=[Message(role="user", content=prompt)],
            temperature=0.0,
            # 256 was too small and failed SILENTLY in the worst way: a
            # reasoning model (DS4, the resident server) spent the whole budget
            # thinking and got truncated before emitting any JSON, so every
            # memory came back with zero entities and no error. Budget for
            # reasoning even when the configured model doesn't do it, since
            # `llamacpp` follows whichever server is resident.
            max_tokens=768,
        )
        parsed = parse_json_object(response)
        allowed = set(catalog)
        proposed = [s for s in (parsed.get("entities") or []) if s in allowed][:6]
        if settings.ontology_extraction_verify:
            alias_map = {
                d["_id"]: d.get("aliases") or []
                async for d in db.ontology_entities.find(
                    {"_id": {"$in": proposed}}, {"aliases": 1}
                )
            }
            slugs = [
                s for s in proposed if verify_slug(s, content, alias_map.get(s))
            ]
            rejected = [s for s in proposed if s not in slugs]
            if rejected:
                logger.debug("extraction rejected (no evidence): %s", rejected)
        else:
            slugs = proposed
    except Exception as exc:  # noqa: BLE001 — extraction is best-effort
        logger.debug("entity extraction failed for %s: %s", memory_id, exc)
        return {"memory_id": memory_id, "entities": [], "detail": f"failed: {exc}"}

    new = sorted(set(slugs) - set(mem.get("entities") or []))
    if not dry_run:
        # entities[] only — no `mentions` edge. See the module docstring.
        # The timestamp is set even when nothing matched: "we looked and found
        # nothing" and "we never looked" must be distinguishable, or a resumed
        # backfill re-pays for every empty result forever.
        update: dict = {"$set": {"entity_extraction_at": datetime.now(timezone.utc)}}
        if new:
            update["$addToSet"] = {"entities": {"$each": new}}
        await db.memories.update_one({"_id": oid}, update)
    return {"memory_id": memory_id, "entities": slugs, "added": new, "dry_run": dry_run}


async def memories_mentioning(
    db: AsyncIOMotorDatabase, slug: str, limit: int = 25
) -> list[dict]:
    """Which memories refer to this entity — the graph->memory direction.

    Reads `memories.entities[]` (the authoritative side) rather than an edge
    collection. Needs the `entities` index created by `ensure_memory_index()`.
    """
    rows = await (
        db.memories.find(
            {"entities": slug, "status": "active"},
            {"content": 1, "categories": 1, "content_type": 1, "created_at": 1},
        )
        .sort("created_at", -1)
        .limit(limit)
        .to_list(length=limit)
    )
    for row in rows:
        row["_id"] = str(row["_id"])
    return rows


# --------------------------------------------------------------------------
# 5c — the forward-only hook
# --------------------------------------------------------------------------

_INDEX_CACHE: dict = {"index": None, "at": 0.0}
_INDEX_TTL_SECONDS = 300.0


async def _cached_index(db: AsyncIOMotorDatabase) -> PathProjectIndex:
    """PathProjectIndex, rebuilt at most every 5 minutes.

    Without the cache this would re-read all 53 projects on every single
    memory write — and memories are written ~670/day by the shell workers
    alone, in bursts.
    """
    import time

    now = time.monotonic()
    if (
        _INDEX_CACHE["index"] is None
        or now - _INDEX_CACHE["at"] > _INDEX_TTL_SECONDS
    ):
        _INDEX_CACHE["index"] = await PathProjectIndex.build(db)
        _INDEX_CACHE["at"] = now
    return _INDEX_CACHE["index"]


async def link_new_memory(
    db: AsyncIOMotorDatabase,
    memory_id: str,
    categories: Optional[list[str]] = None,
) -> list[str]:
    """§7 phase 5c — link one freshly-written memory into the graph.

    Two tiers, cheapest first:
      1. ALWAYS: the deterministic path-category mapping. No inference, one
         indexed lookup, microseconds.
      2. ONLY if `ontology_extraction_enabled`: an LLM pass. Off by default
         because it spends inference on every memory written.

    Total by construction — never raises. This runs off the memory-creation
    path, and a cross-link failure must never cost Ben a memory.
    """
    linked: list[str] = []
    try:
        index = await _cached_index(db)
        slugs = {
            slug
            for cat in (categories or [])
            if looks_like_path(cat)
            for slug in [index.owner(cat)]
            if slug
        }
        if slugs:
            await db.memories.update_one(
                {"_id": ObjectId(memory_id)},
                {"$addToSet": {"entities": {"$each": sorted(slugs)}}},
            )
            linked.extend(sorted(slugs))

        if settings.ontology_extraction_enabled:
            result = await extract_entities_for_memory(db, memory_id)
            if result:
                linked.extend(result.get("added") or [])
    except Exception as exc:  # noqa: BLE001 — cross-link is never load-bearing
        logger.debug("cross-link failed for memory %s: %s", memory_id, exc)
    return linked


async def ensure_memory_index(db: AsyncIOMotorDatabase) -> None:
    """Index on memories.entities — what makes the cross-link queryable at all.

    Without it, `memories_mentioning` is a collection scan over 14,590 docs.
    Idempotent; cheap enough to call on every boot.
    """
    try:
        await db.memories.create_index("entities")
    except Exception as exc:  # noqa: BLE001 — index bootstrap is advisory
        logger.warning("memories.entities index bootstrap failed: %s", exc)


async def run_targeted_backfill(
    db: AsyncIOMotorDatabase,
    *,
    dry_run: bool = True,
    limit: Optional[int] = None,
) -> dict:
    """§7 piece 2 (Phase 5d) — the ~919 curated memories, and only those.

    The `$nin` on BULK_SOURCE_TYPES is the enforcement point for the phase-5e
    decision. Widening it to the whole store is a multi-hour local-GPU job that
    was explicitly ruled out; if that ever changes it should be a deliberate
    edit here, not an accidental one.
    """
    import time

    catalog = await _entity_catalog(db)
    scope = {
        "source.type": {"$nin": list(BULK_SOURCE_TYPES)},
        "status": "active",
    }
    # Resumable: skip anything already looked at. At ~7-30s per memory this
    # job runs for hours, so it MUST survive being interrupted — the first
    # attempt at it was killed partway through and would otherwise have
    # restarted from zero.
    query = {**scope, "entity_extraction_at": {"$exists": False}}
    eligible = await db.memories.count_documents(scope)
    remaining = await db.memories.count_documents(query)

    cursor = db.memories.find(query, {"_id": 1}).sort("created_at", -1)
    if limit:
        cursor = cursor.limit(limit)

    started = time.monotonic()
    processed = labelled = failed = 0
    async for mem in cursor:
        result = await extract_entities_for_memory(
            db, str(mem["_id"]), catalog=catalog, dry_run=dry_run
        )
        processed += 1
        if result and result.get("entities"):
            labelled += 1
        elif result and result.get("detail", "").startswith("failed"):
            failed += 1
        if processed % 25 == 0:
            rate = (time.monotonic() - started) / processed
            logger.info(
                "ontology backfill: %s/%s processed (%.1fs/memory, %s labelled)",
                processed, remaining, rate, labelled,
            )

    elapsed = time.monotonic() - started
    return {
        "dry_run": dry_run,
        "eligible_total": eligible,
        "remaining_before_run": remaining,
        "processed": processed,
        "labelled": labelled,
        "failed": failed,
        "seconds_per_memory": round(elapsed / processed, 1) if processed else None,
        "excluded_source_types": list(BULK_SOURCE_TYPES),
        "note": (
            "shell_extraction + claude_session_digest are excluded by decision "
            "(closed 2026-08-07). "
            "Resumable: processed memories carry entity_extraction_at."
        ),
        "at": datetime.now(timezone.utc).isoformat(),
    }
