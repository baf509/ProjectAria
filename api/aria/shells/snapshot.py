"""
ARIA - Watched Shells Snapshot Worker

Purpose: Periodically capture the visible pane of each active shell so that
redraws missed by pipe-pane are still queryable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aria.config import settings
from aria.shells.service import ShellService

logger = logging.getLogger(__name__)


class SnapshotWorker:
    """Background task that snapshots active shells on a fixed interval."""

    def __init__(self, service: ShellService):
        self.service = service
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="shells.snapshot")
        logger.info("shells snapshot worker started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        interval = max(5, int(settings.shells_snapshot_interval_seconds))
        reconcile_every = max(1, int(settings.shells_reconcile_interval_seconds / interval) or 1)
        ticks = 0
        while not self._stop.is_set():
            try:
                await self._tick()
                ticks += 1
                if ticks % reconcile_every == 0:
                    await self.service.reconcile_statuses()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("shells snapshot tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        shells = await self.service.list_shells(status=["active", "idle"])
        for shell in shells:
            await self.service.capture_and_snapshot(shell.name)
