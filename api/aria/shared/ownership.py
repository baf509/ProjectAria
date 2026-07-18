"""
ARIA - Shared Services · S3: Freshness & Ownership convention

The one rule that makes hybrid human/agent + machine data safe, used by the scan
worker (S2), the Ontology graph, and mirrored by Coherence C6/C3:

    - The worker writes ONLY worker-owned (structural/derived) fields.
    - Human/agent-owned fields (prose, aliases, tags, curated edges) are never
      overwritten.
    - Per-field `source` provenance records who set each value.
    - On contradiction between curated and observed, we FLAG for review rather
      than clobber (see review.py).
"""
from datetime import datetime, timezone
from typing import Iterable


def merge_owned(
    existing: dict,
    observed: dict,
    *,
    worker_fields: Iterable[str],
    actor: str = "scan-worker",
) -> tuple[dict, list[str]]:
    """
    Produce a `$set` update that touches ONLY worker-owned fields, and report any
    fields where the observed value contradicts a human/agent-curated value.

    Args:
        existing: the current document (may be {} for a fresh entity)
        observed: freshly observed structural values
        worker_fields: the set of field names the worker is allowed to own
        actor: provenance label written into `source.<field>`

    Returns:
        (set_update, conflicts) where `set_update` is a dict safe to `$set`
        (worker-owned fields + provenance + last_verified_at) and `conflicts` is
        a list of field names a human curated to a different value (never
        auto-overwritten — surface via review.py).
    """
    worker_fields = set(worker_fields)
    now = datetime.now(timezone.utc)
    set_update: dict = {}
    conflicts: list[str] = []

    provenance = dict(existing.get("source") or {})

    for field, value in observed.items():
        if field not in worker_fields:
            # Not the worker's to write — if a human already set a different
            # value, that's a conflict to review, not to overwrite.
            if field in existing and existing[field] not in (None, "", [], {}) and existing[field] != value:
                owner = provenance.get(field, {}).get("actor") if isinstance(provenance.get(field), dict) else None
                if owner and owner != actor:
                    conflicts.append(field)
            continue
        if existing.get(field) != value:
            set_update[field] = value
            provenance[field] = {"actor": actor, "at": now}

    if set_update:
        set_update["source"] = provenance
        set_update["last_verified_at"] = now

    return set_update, conflicts
