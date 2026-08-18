"""
ARIA - Watched Shells Memory Extraction Worker

Purpose: Periodically scans recent shell_events for each watched shell and
feeds their concatenated text through MemoryExtractor to mint long-term
memories from coding session conversations.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from aria.config import settings
from aria.memory.extraction import MemoryExtractor
from aria.shells.service import ShellService

logger = logging.getLogger(__name__)


class ShellExtractionWorker:
    """Background worker that extracts memories from shell event streams."""

    def __init__(
        self,
        shell_service: ShellService,
        memory_extractor: MemoryExtractor,
    ):
        self.shell_service = shell_service
        self.memory_extractor = memory_extractor
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="shells.extraction")
        logger.info("shells extraction worker started")

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
        interval_min = max(1, int(settings.shells_extraction_interval_minutes or 10))
        interval = interval_min * 60
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # pragma: no cover
                logger.warning("shells extraction tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        state_coll = self.shell_service.db.shell_extraction_state
        # Includes "stopped" (Coherence C2b fix, 2026-07-31): this used to be
        # ["active", "idle"] only, so a shell that stopped before its next
        # tick landed was excluded from extraction PERMANENTLY -- there is no
        # path back from "stopped" to being reconsidered. Since the large
        # majority of real shell history is status=stopped, this meant most
        # coding-session conversations were never mined for memories at all
        # (confirmed live: 215 shells with real events, some with 12,000+,
        # had never been touched). Extraction is idempotent per-shell via the
        # line-number cursor, so including stopped shells here just means a
        # shell keeps getting swept until its cursor catches up, same as any
        # other status.
        # Only shells with unextracted work. The sweep used to be the whole
        # fleet -- ~215 shells, mostly stopped and long since caught up --
        # each costing a state find_one AND a list_events that returned
        # nothing: ~430 queries per tick to do, usually, no work at all.
        shells = await self.shell_service.list_shells(
            status=["active", "idle", "stopped"],
            pending_min=int(settings.shells_extraction_min_events or 20),
        )

        for shell in shells:
            await self._process_shell(shell, state_coll)

    async def _mirror_cursor(self, shell_name: str, line: int) -> None:
        """Mirror the extraction cursor onto the shell doc.

        shell_extraction_state stays authoritative; this copy exists so
        _tick can ask Mongo "which shells have work?" instead of asking every
        shell in turn. Written on every outcome -- including "already caught
        up" -- so a fleet that predates the mirror self-corrects on its first
        sweep rather than needing a migration.
        """
        try:
            await self.shell_service.shells.update_one(
                {"name": shell_name},
                {"$set": {"last_extracted_line": int(line)}},
            )
        except Exception as exc:  # never let bookkeeping break extraction
            logger.debug("cursor mirror failed for %s: %s", shell_name, exc)

    async def _process_shell(self, shell, state_coll, *, force_local: bool = False, claude_model: Optional[str] = None) -> int:
        """Extract one chunk (<=1000 events) from a single shell's unextracted
        tail. Returns the number of events consumed (0 if skipped: cursor
        already caught up, or fewer than the min-events threshold available).
        Shared by the periodic tick (one chunk per shell per interval) and
        backfill() (loops this per shell until caught up).

        force_local routes extraction to the local "agentic" model
        (chadrockv2, ARIA's own dedicated coding-model server) instead of the
        Claude CLI runner -- deliberately NOT "llamacpp" (that's Hermes's own
        chat model, qwen3.6-35b-a3b; using it here would contend with live
        Hermes chat traffic on the same server)."""
        min_events = int(settings.shells_extraction_min_events or 20)
        state = await state_coll.find_one({"shell_name": shell.name}) or {}
        since_line = int(state.get("last_line_extracted", 0))

        # Self-heal a stale cursor. line_number is handed out from the
        # shell's line_count counter; if that counter was reset (events
        # pruned/recaptured, shell re-registered) the saved cursor can sit
        # above the current max line, so the `> since_line` filter would
        # hide every event forever and extraction would never run again.
        # Clamp back down so we resume from what's actually there.
        if since_line > shell.line_count:
            logger.warning(
                "shells extraction: cursor for %s (%d) exceeds line_count "
                "(%d); resetting to resume",
                shell.name, since_line, shell.line_count,
            )
            since_line = shell.line_count
            await self._mirror_cursor(shell.name, since_line)

        events = await self.shell_service.list_events(
            shell.name,
            since_line=since_line,
            limit=1000,
            kinds=["output", "input"],
        )
        if len(events) < min_events:
            # Caught up (or not enough new lines yet): record where we are so
            # this shell drops out of the next tick's query.
            await self._mirror_cursor(shell.name, since_line)
            return 0

        lines = []
        for ev in events:
            prefix = "> " if ev.kind == "input" else ""
            lines.append(f"{prefix}{ev.text_clean}")
        text = "\n".join(lines).strip()
        if not text:
            return 0

        try:
            # Bound each call: a hung backend must never stall the worker's
            # heartbeat (a multi-hour selfcheck flatline once traced to an
            # unbounded llama.cpp request). On timeout we skip this shell and
            # retry it next tick — the cursor only advances on success.
            extracted = await asyncio.wait_for(
                self.memory_extractor.extract_from_text(
                    text,
                    llm_backend=settings.shells_extraction_backend,
                    llm_model=settings.shells_extraction_model,
                    force_local=force_local,
                    claude_model=claude_model,
                ),
                timeout=settings.shells_extraction_timeout_seconds,
            )
            logger.info(
                "shells extraction: %s → %d memories", shell.name, len(extracted)
            )
        except asyncio.TimeoutError:
            logger.warning(
                "shells extract timed out for %s after %ss; skipping this tick",
                shell.name, settings.shells_extraction_timeout_seconds,
            )
            return 0
        except Exception as exc:
            logger.warning("shells extract failed for %s: %s", shell.name, exc)
            return 0

        # Persist (Coherence C2a fix, 2026-07-29): `extracted` used to be
        # logged and dropped here — a whole memory stream discarded every
        # tick for every watched shell. Same create_memory call pattern as
        # MemoryExtractor.extract_from_conversation().
        for memory_data in extracted:
            try:
                await self.memory_extractor.long_term_memory.create_memory(
                    content=memory_data["content"],
                    content_type=memory_data.get("content_type", "fact"),
                    categories=memory_data.get("categories", []),
                    importance=memory_data.get("importance", 0.5),
                    confidence=0.8,  # auto-extracted, moderate confidence
                    source={
                        "type": "shell_extraction",
                        "shell_name": shell.name,
                        "extracted_at": datetime.now(timezone.utc),
                    },
                )
            except Exception as exc:
                logger.warning(
                    "shells extraction: failed to persist a memory for %s: %s",
                    shell.name, exc,
                )

        await state_coll.update_one(
            {"shell_name": shell.name},
            {
                "$set": {
                    "shell_name": shell.name,
                    "last_line_extracted": events[-1].line_number,
                    "last_run_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        await self._mirror_cursor(shell.name, events[-1].line_number)
        return len(events)

    async def backfill(self, *, force_local: bool = False, claude_model: Optional[str] = None) -> dict:
        """Drain every STOPPED shell's unextracted backlog to completion, not
        just the one chunk-per-tick the periodic worker does. For a shell
        with tens of thousands of events, waiting on the 1000-event-per-tick,
        10-minute-interval cadence would take most of a day; this loops
        _process_shell per shell until its cursor catches up (or a chunk
        makes no progress), so a one-time historical catch-up finishes in
        one pass instead of spread across however many ticks it'd otherwise
        take. Safe to re-run: purely cursor-driven, same as the tick path.

        Deliberately excludes active/idle shells (unlike _tick()'s status
        list): those are already served by the periodic tick as they grow,
        and this method's "loop until a chunk makes no progress" approach
        assumes a FIXED backlog. Tried including them once -- an active
        shell's line_count kept growing while the loop held a stale copy of
        it, so the self-heal-a-stale-cursor logic in _process_shell kept
        firing and resetting the cursor to the same stale point every pass:
        an infinite loop that burned real LLM calls with zero progress until
        killed. Stopped shells have a frozen line_count, so no such race.
        """
        state_coll = self.shell_service.db.shell_extraction_state
        shells = await self.shell_service.list_shells(status=["stopped"])

        # Defense in depth even with the active/idle exclusion above: cap
        # chunks per shell so a future edge case (e.g. a shell reactivated
        # mid-backfill) degrades to "stops early" rather than "loops forever
        # burning LLM calls" again.
        max_chunks_per_shell = 200  # 200k events at the 1000-event chunk size

        summary = {"shells_scanned": len(shells), "shells_processed": 0, "chunks_processed": 0, "events_consumed": 0, "shells_capped": 0}
        for shell in shells:
            processed_this_shell = False
            chunks_this_shell = 0
            while chunks_this_shell < max_chunks_per_shell:
                consumed = await self._process_shell(shell, state_coll, force_local=force_local, claude_model=claude_model)
                if consumed == 0:
                    break
                processed_this_shell = True
                chunks_this_shell += 1
                summary["chunks_processed"] += 1
                summary["events_consumed"] += consumed
            if chunks_this_shell >= max_chunks_per_shell:
                summary["shells_capped"] += 1
                logger.warning(
                    "shells extraction backfill: %s hit the %d-chunk safety cap, stopping early",
                    shell.name, max_chunks_per_shell,
                )
            if processed_this_shell:
                summary["shells_processed"] += 1
                logger.info("shells extraction backfill: caught up %s", shell.name)
        return summary
