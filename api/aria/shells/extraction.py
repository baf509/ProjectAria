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
        min_events = int(settings.shells_extraction_min_events or 20)
        state_coll = self.shell_service.db.shell_extraction_state
        shells = await self.shell_service.list_shells(status=["active", "idle"])

        for shell in shells:
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

            events = await self.shell_service.list_events(
                shell.name,
                since_line=since_line,
                limit=1000,
                kinds=["output", "input"],
            )
            if len(events) < min_events:
                continue

            lines = []
            for ev in events:
                prefix = "> " if ev.kind == "input" else ""
                lines.append(f"{prefix}{ev.text_clean}")
            text = "\n".join(lines).strip()
            if not text:
                continue

            try:
                # Bound each call: a hung backend must never stall the worker's
                # heartbeat (a multi-hour selfcheck flatline once traced to an
                # unbounded llama.cpp request). On timeout we skip this shell and
                # retry it next tick — the cursor only advances on success.
                extracted = await asyncio.wait_for(
                    self.memory_extractor.extract_from_text(text),
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
                continue
            except Exception as exc:
                logger.warning("shells extract failed for %s: %s", shell.name, exc)
                continue

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
