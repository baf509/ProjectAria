"""
ARIA - Watched Shells Idle Notifier

Purpose: Detects shells idling at an interactive prompt (e.g. Claude "yes/no?"
dialogs) and fires a Signal/Telegram notification via NotificationService.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from aria.config import settings
from aria.notifications.service import NotificationService
from aria.shells.ansi import matches_prompt, parse_prompt_patterns
from aria.shells.service import ShellService

logger = logging.getLogger(__name__)


class IdleNotifier:
    """Background worker that watches shells and notifies on idle-at-prompt."""

    def __init__(
        self,
        shell_service: ShellService,
        notification_service: NotificationService,
    ):
        self.shell_service = shell_service
        self.notification_service = notification_service
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._last_notified: dict[str, int] = {}  # name -> last line_number notified
        self._patterns = parse_prompt_patterns(settings.shells_idle_prompt_patterns)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="shells.notifier")
        logger.info("shells idle notifier started")

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
        interval = max(5, int(settings.shells_idle_notifier_interval_seconds or 30))
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # pragma: no cover
                logger.warning("shells notifier tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        threshold = int(settings.shells_idle_threshold_seconds or 60)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=threshold)
        shells = await self.shell_service.list_shells(status=["active", "idle"])
        for shell in shells:
            last_activity = shell.last_activity_at
            if last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)
            if last_activity > cutoff:
                continue
            tail = await self.shell_service.tail(shell.name, lines=5)
            if not tail:
                continue
            last = tail[-1]
            if last.kind != "output":
                continue
            if self._last_notified.get(shell.name) == last.line_number:
                continue
            if not matches_prompt(last.text_clean, self._patterns):
                continue
            await self.notification_service.notify(
                source="shells",
                event_type="idle_prompt",
                detail=f"{shell.short_name or shell.name} awaiting input: {last.text_clean[:160]}",
                cooldown_seconds=300,
            )
            self._last_notified[shell.name] = last.line_number
