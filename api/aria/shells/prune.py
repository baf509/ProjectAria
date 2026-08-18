"""Watched Shells — scrollback retention by per-shell token budget.

Raw shell_events are unbounded by default and dominated by tiny TUI-redraw
fragments. This keeps only the most recent ~N tokens of scrollback per shell.
Derived data (memories, projects, tasks) is never touched — only raw events.

Retention is a *token budget per shell*, not a time TTL: we walk each shell's
events newest→oldest, sum their text length, and drop everything older than the
budget. We never delete an event at/above the shell's extraction cursor, so the
memory-extraction worker can never lose un-processed input.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aria.config import settings

logger = logging.getLogger(__name__)

# Rough chars→tokens ratio. Scrollback is mostly ASCII; ~4 chars/token is a
# safe overestimate (keeps slightly more than the nominal token budget).
CHARS_PER_TOKEN = 4


async def prune_shell_events(
    db,
    budget_tokens: int,
    *,
    dry_run: bool = False,
    protect_unextracted: bool = True,
    within_budget: Optional[dict[str, int]] = None,
) -> dict[str, int]:
    """Trim each shell's events down to the most recent ``budget_tokens``.

    Returns a mapping of shell_name -> number of events deleted (or, in
    dry_run mode, the number that *would* be deleted).

    `within_budget` is an optional caller-owned memo of shell_name ->
    line_count-at-the-time-it-was-found-within-budget. A shell that was
    within budget and has not grown since is skipped without running its
    aggregation; it is updated in place. Pass None to force a full pass.
    """
    budget_chars = max(0, int(budget_tokens)) * CHARS_PER_TOKEN

    cursors: dict[str, int] = {}
    if protect_unextracted:
        async for s in db.shell_extraction_state.find(
            {}, {"shell_name": 1, "last_line_extracted": 1}
        ):
            cursors[s["shell_name"]] = int(s.get("last_line_extracted", 0))

    # Shell names come from the `shells` registry, not from a distinct() on
    # shell_events -- that was a full COLLSCAN of an 18.5M-row collection
    # every 6 hours just to learn a list of a few hundred names.
    #
    # Trade-off: a shell present in shell_events but absent from `shells`
    # (orphaned by a manual delete) is no longer pruned. `shells` is the
    # registry of record and the adopt/reconcile workers surface orphans.
    line_counts: dict[str, int] = {}
    async for s in db.shells.find({}, {"name": 1, "line_count": 1}):
        if s.get("name"):
            line_counts[s["name"]] = int(s.get("line_count") or 0)
    shell_names = list(line_counts)
    results: dict[str, int] = {}

    for name in shell_names:
        # Amortize: a shell that was within budget last pass and has not grown
        # cannot have crossed it, so skip its aggregation entirely.
        if within_budget is not None:
            seen_at = within_budget.get(name)
            if seen_at is not None and line_counts.get(name, 0) <= seen_at:
                results[name] = 0
                continue
        # Find the highest line_number whose cumulative (newest→oldest) text
        # length has already exceeded the budget; everything <= that line is
        # older than the budget window and eligible for deletion.
        pipeline = [
            {"$match": {"shell_name": name}},
            {
                "$setWindowFields": {
                    "partitionBy": "$shell_name",
                    "sortBy": {"line_number": -1},
                    "output": {
                        "cum": {
                            # Computed inline, NOT via a preceding $project:
                            # projecting first was measured 2.4x SLOWER on the
                            # multi-million-event shells (21.2s vs 8.8s on
                            # claude-infrastructure, 2026-08-18).
                            "$sum": {"$strLenCP": {"$ifNull": ["$text_clean", ""]}},
                            "window": {"documents": ["unbounded", "current"]},
                        }
                    },
                }
            },
            {"$match": {"cum": {"$gt": budget_chars}}},
            {"$group": {"_id": None, "cutoff": {"$max": "$line_number"}}},
        ]

        cutoff: Optional[int] = None
        async for d in db.shell_events.aggregate(pipeline, allowDiskUse=True):
            cutoff = d.get("cutoff")

        if cutoff is None:  # shell is within budget; nothing to prune
            if within_budget is not None:
                within_budget[name] = line_counts.get(name, 0)
            results[name] = 0
            continue

        # Only protect un-extracted events when extraction has actually made
        # progress on this shell. If the cursor is 0 / absent (extraction off or
        # never run), clamping to it would block pruning forever and the shell
        # would grow unbounded — so fall back to the pure token budget.
        if protect_unextracted and cursors.get(name, 0) > 0:
            cutoff = min(cutoff, cursors[name])

        if cutoff <= 0:
            results[name] = 0
            continue

        flt = {"shell_name": name, "line_number": {"$lte": cutoff}}
        if dry_run:
            results[name] = await db.shell_events.count_documents(flt)
        else:
            res = await db.shell_events.delete_many(flt)
            results[name] = res.deleted_count

    return results


class ShellEventsPruneWorker:
    """Background worker that periodically enforces the per-shell token budget."""

    def __init__(self, db):
        self.db = db
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # shell_name -> line_count when it was last found within budget. A
        # shell that has not grown since cannot have crossed the budget, so
        # its aggregation is skipped. Bounded by the fleet size.
        self._within_budget: dict[str, int] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="shells.prune")
        logger.info(
            "shells prune worker started (budget=%d tok, every %dh)",
            settings.shells_event_token_budget,
            settings.shells_prune_interval_hours,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        interval = max(1, int(settings.shells_prune_interval_hours)) * 3600
        # Defer the first run so startup stays light.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=120)
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                res = await prune_shell_events(
                    self.db,
                    settings.shells_event_token_budget,
                    within_budget=self._within_budget,
                )
                deleted = {k: v for k, v in res.items() if v}
                if deleted:
                    logger.info(
                        "shells prune: deleted %d events %s",
                        sum(deleted.values()),
                        deleted,
                    )
            except Exception as exc:  # pragma: no cover
                logger.warning("shells prune tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
