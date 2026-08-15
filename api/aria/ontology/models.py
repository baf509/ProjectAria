"""
ARIA - Ontology data model

Phase: Ontology Memory Map · Phase 1
Purpose: Entity/relation vocabulary, slug rules, and field-ownership sets.

Related Spec Sections:
- ONTOLOGY_MEMORY_DESIGN.md §3 (data model), §10 (notes)
"""

from __future__ import annotations

import re
from typing import Final

# --- Entity types (§3) -----------------------------------------------------
ENTITY_TYPES: Final[tuple[str, ...]] = (
    "machine",
    "device",
    "service",
    "project",
    "datastore",
    "network",
    "external_service",
    "person",
)

# --- Predicate vocabulary (§3) --------------------------------------------
# `mentions` is the memory->entity edge that carries the §7 cross-link; every
# other predicate is entity->entity.
PREDICATES: Final[tuple[str, ...]] = (
    "runs_on",
    "hosts",
    "part_of",
    "depends_on",
    "connects_to",
    "proxies_to",
    "deploys_to",
    "backs_up_to",
    "client_of",
    "member_of",
    "stores_in",
    "serves",
    "mentions",
)

# Genuine inverse pairs — used to answer "what depends on X?" from either
# direction without storing both edges.
#
# `member_of` and `part_of` are deliberately NOT here. They look like a pair
# but are near-synonyms (both mean "X is inside Y"), so treating them as
# inverses would flip an edge's direction on every traversal: `corsair-ai
# member_of tailnet` would read back as `tailnet part_of corsair-ai`. Their
# real inverses would be has_member/has_part, which this vocabulary does not
# have and does not need.
INVERSE: Final[dict[str, str]] = {
    "runs_on": "hosts",
    "hosts": "runs_on",
    "depends_on": "serves",
    "serves": "depends_on",
}

ENTITIES_COLLECTION: Final[str] = "ontology_entities"
RELATIONS_COLLECTION: Final[str] = "ontology_relations"

# --- Field ownership (§3, S3 convention) ----------------------------------
# The worker may refresh these from live state on every reconcile.
WORKER_FIELDS: Final[tuple[str, ...]] = (
    "attributes",
    "status",
    "name",
)
# These are Ben's or a curating agent's. A projection must NEVER overwrite
# them; a contradiction goes to the S3 review queue instead. This is the same
# rule as the ObsidianWriter's human-edit guard and C3's propose-don't-clobber.
PROTECTED_FIELDS: Final[tuple[str, ...]] = (
    "aliases",
    "summary",
    "tags",
)

_SLUG_SAFE = re.compile(r"[^a-z0-9._@-]+")


def is_valid_type(value: str) -> bool:
    return value in ENTITY_TYPES


def is_valid_predicate(value: str) -> bool:
    return value in PREDICATES


def normalize_name(name: str) -> str:
    """Lowercase, collapse anything unsafe to a single hyphen.

    `@` and `.` survive because real names use them (`restic-repo@nas`,
    `aria-api.service`); keeping them makes slugs readable rather than
    hash-like, which is the whole point of a slug `_id`.
    """
    slug = _SLUG_SAFE.sub("-", (name or "").strip().lower())
    return slug.strip("-") or "unnamed"


def entity_slug(entity_type: str, name: str) -> str:
    """Canonical `type:name` id (§3).

    Stable and human-readable so the same entity can be referenced by hand,
    from another machine, or from a memory's `entities[]` without a lookup.
    """
    if not is_valid_type(entity_type):
        raise ValueError(
            f"unknown entity type {entity_type!r} (known: {', '.join(ENTITY_TYPES)})"
        )
    return f"{entity_type}:{normalize_name(name)}"


def split_slug(slug: str) -> tuple[str, str]:
    """('machine', 'corsair-ai') for 'machine:corsair-ai'."""
    entity_type, _, name = (slug or "").partition(":")
    return entity_type, name


def project_entity_slug(doc: dict) -> str:
    """Entity slug for a `db.projects` document.

    Prefers the project's own `slug` field over its display `name`: the slug is
    the project's identity and survives a rename, whereas deriving from `name`
    would silently mint a second entity the moment a project is retitled.

    MUST be the single source of this mapping — the projection and the
    path-category cross-link both call it. If they ever derived slugs
    differently, `entities[]` would point at entities the graph does not have.
    """
    return entity_slug("project", doc.get("slug") or doc.get("name") or "")


def project_roots(doc: dict) -> list[str]:
    """Filesystem roots that identify a project, `path` first.

    Reads `relevant_paths` as well as `path` because they are not
    interchangeable in practice: the harvested "ARIA" row carries only
    `relevant_paths`, so a `path`-only reader misses it entirely.
    """
    roots: list[str] = []
    for raw in [doc.get("path"), *(doc.get("relevant_paths") or [])]:
        if raw and raw not in roots:
            roots.append(raw)
    return roots
