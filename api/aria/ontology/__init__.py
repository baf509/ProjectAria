"""
ARIA - Ontology Memory Map

Phase: Ontology Memory Map · Phases 1-5
Purpose: A queryable knowledge graph of Ben's world (machines, services,
projects, network, devices) cross-linked to the flat semantic memory store.

Related Spec Sections:
- vault/ProjectAria/Design/ARCHITECTURE.md (Ontology Memory Map)
  — data model, HTTP API, and the memory<->graph cross-link)

Design rule that shapes every module here (§4): **project what churns,
hand-author what doesn't.** Services and projects are derived from the
collections that already own them; only the durable physical world (machines,
devices, network, person) is hand-written. The original plan hand-seeded
services and the list was stale within three weeks.
"""

from aria.ontology.models import (
    ENTITY_TYPES,
    PREDICATES,
    entity_slug,
    is_valid_predicate,
    is_valid_type,
)
from aria.ontology.store import OntologyStore

__all__ = [
    "ENTITY_TYPES",
    "PREDICATES",
    "OntologyStore",
    "entity_slug",
    "is_valid_predicate",
    "is_valid_type",
]
