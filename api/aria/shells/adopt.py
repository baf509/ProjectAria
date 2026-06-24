"""Watched Shells — auto-adopt reconciler.

Real-time adoption of externally-started `claude-*` tmux sessions is handled by
the tmux hooks in scripts/aria-tmux-hook.conf (session-created / client-attached
→ aria-shell-register --ensure-capture). This worker is the backstop: it polls
the live session list and ensures each is registered and captured, catching the
cases the hook can't — sessions that already existed before the hook was
installed, or whose capture process died and was never reattached.

Cheap and idempotent: ShellService.reconcile_adopt() only starts a pipe-pane for
sessions whose capture pidfile is dead, so a steady-state tick is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aria.config import settings

logger = logging.getLogger(__name__)


class ShellAdoptWorker:
    """Periodically reconcile live tmux sessions into watched, captured shells."""

    def __init__(self, service, interval_seconds: Optional[int] = None):
        self.service = service
        self.interval = max(5, int(interval_seconds or settings.shells_adopt_interval_seconds))
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="shells.adopt")
        logger.info("shells adopt reconciler started (every %ds)", self.interval)

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
        while not self._stop.is_set():
            try:
                started = await self.service.reconcile_adopt()
                if started:
                    logger.info("shells adopt: (re)started capture for %d session(s)", started)
            except Exception as exc:  # pragma: no cover
                logger.warning("shells adopt tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
