"""
ARIA - Ontology scan emitter

Phase: Ontology Memory Map · Phase 5a
Purpose: Keep the projected half of the graph current, riding the existing S2
scan/reconcile worker rather than adding a second scanner.

Related Spec Sections:
- ONTOLOGY_MEMORY_DESIGN.md §4a, §5a (phase table), §7
- aria/shared/scan.py (the S2 substrate this registers on)

`always_run = True` matters. The default emitter contract fires only when the
machine snapshot diffs (containers/services/ports changed). This projection's
inputs are db.projects and the two registries, which change without any of
that — a new project harvested, a service added to the registry. Gating on a
machine-state diff would mean the graph refreshed only by coincidence, so this
emitter opts into running every tick.
"""

from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.ontology.projection import OntologyProjector

logger = logging.getLogger(__name__)


class OntologyProjectionEmitter:
    """Re-derive `service` / `project` / `machine` entities on every scan tick.

    Cheap by construction: no LLM, and embedding is off by default
    (`ontology_projection_embed`) because re-embedding 100 entities costs ~78s
    on the CPU embedding service — fine for an explicit run, far too slow for
    a periodic one.
    """

    # Opt into running regardless of the snapshot diff. See module docstring.
    always_run = True

    def __init__(self, embed: bool = False):
        self.embed = embed

    async def emit(
        self, db: AsyncIOMotorDatabase, snapshot: dict, diff: dict
    ) -> None:
        # snapshot/diff are deliberately ignored: this projection reads its own
        # sources (registries + db.projects + db.nodes), exactly as
        # GitChangeEmitter runs its own per-repo cursor.
        result = await OntologyProjector(db).run_all(embed=self.embed)
        counts = result.get("counts", {})
        logger.debug(
            "ontology projection: %s entities, %s relations",
            counts.get("entities"),
            counts.get("relations"),
        )
