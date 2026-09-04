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
# Bound each server-side scan so one giant historical shell cannot monopolize
# Mongo's only Lima vCPU for ten seconds at a time.
SCAN_PAGE_EVENTS = 25_000


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
        # Walk the existing (shell_name, line_number) index newest-first in
        # bounded SERVER-SIDE pages. The previous $setWindowFields plan always
        # examined every event in the shell: 6.2M docs / 10 CPU-seconds / 1.1
        # GiB read for one shell. A first indexed-cursor fix stopped at 175K
        # docs, but streaming those tiny redraw fragments into Python still
        # pegged ARIA + Mongo. Here Mongo returns one sum per 25K rows; only the
        # final crossing page pays a bounded window calculation.
        cutoff: Optional[int] = None
        remaining = budget_chars
        before_line: Optional[int] = None
        while cutoff is None:
            match: dict = {"shell_name": name}
            if before_line is not None:
                match["line_number"] = {"$lt": before_line}
            prefix = [
                {"$match": match},
                {"$sort": {"line_number": -1}},
                {"$limit": SCAN_PAGE_EVENTS},
            ]
            meta_pipeline = prefix + [{
                "$group": {
                    "_id": None,
                    "chars": {"$sum": {"$strLenCP": {"$ifNull": ["$text_clean", ""]}}},
                    "oldest": {"$min": "$line_number"},
                    "count": {"$sum": 1},
                }
            }]
            page = await db.shell_events.aggregate(meta_pipeline).to_list(length=1)
            if not page:
                break
            meta = page[0]
            page_chars = int(meta.get("chars") or 0)
            page_count = int(meta.get("count") or 0)
            oldest = int(meta.get("oldest") or 0)
            if page_chars <= remaining:
                remaining -= page_chars
                if page_count < SCAN_PAGE_EVENTS or oldest <= 0:
                    break
                before_line = oldest
                continue

            crossing_pipeline = prefix + [
                {
                    "$setWindowFields": {
                        "sortBy": {"line_number": -1},
                        "output": {
                            "cum": {
                                "$sum": {"$strLenCP": {"$ifNull": ["$text_clean", ""]}},
                                "window": {"documents": ["unbounded", "current"]},
                            }
                        },
                    }
                },
                {"$match": {"cum": {"$gt": remaining}}},
                {"$limit": 1},
                {"$project": {"_id": 0, "cutoff": "$line_number"}},
            ]
            crossing = await db.shell_events.aggregate(crossing_pipeline).to_list(length=1)
            if crossing:
                cutoff = int(crossing[0].get("cutoff") or 0)
            break

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
