"""
ARIA - Paused-shell nudge worker

Purpose: own the timer that sweeps the fleet for shells sitting blocked at a
prompt and nudges each one, so the three-strikes escalation in
`api/routes/shell_nudge.py` actually fires.

That timer was a Hermes cron ("ARIA paused-shell nudger", */15, 580 runs) whose
prompt did nothing but call `fleet_status` and then `nudge_paused_shell` per
blocked shell — an LLM asked to do the job of a for-loop. On 2026-08-10 its
model (gemma :8104) went away, the job errored, the gateway paused it, and no
shell has been nudged since. The state, the debounce, the protected tag and the
three-strikes alert all lived in ARIA already; only the loop was over there.

So this worker is deliberately thin: it iterates and calls the exact same
`nudge_shell_once()` the HTTP route calls. There is no policy here — put policy
in the shared path or the two callers will drift, which is how the cron ended up
with its own stale copy of the rules.

Off by default (`shells_nudge_worker_enabled`); the cron it replaces is already
paused, so enabling this is what restores the behaviour.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from aria.config import settings

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NudgeWorker:
    """Timer worker: sweep blocked shells, nudge each once per pass."""

    def __init__(
        self,
        db,
        shell_service=None,
        notifier=None,
        *,
        interval_minutes: Optional[int] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.db = db
        self._shell_service = shell_service
        self._notifier = notifier
        minutes = interval_minutes
        if minutes is None:
            minutes = getattr(settings, "shells_nudge_worker_interval_minutes", 15)
        # Floor of one minute: the shared path debounces per shell
        # (`shells_nudge_min_interval_minutes`, 10) and refuses a freshly-paused
        # one (5 min), so a faster sweep only burns fleet_overview calls.
        self.interval = max(60, int(minutes) * 60)
        self._now = now or _utcnow
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last: dict = {"checked_at": None, "reason": "not_run"}

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        if not getattr(settings, "shells_nudge_worker_enabled", False):
            logger.info("nudge worker disabled by settings")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="shells.nudge_worker")
        logger.info("nudge worker started (every %ds)", self.interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    def status(self) -> dict:
        return {
            "enabled": bool(getattr(settings, "shells_nudge_worker_enabled", False)),
            "running": self._task is not None and not self._task.done(),
            "interval_seconds": self.interval,
            "last": dict(self._last),
        }

    async def _run(self) -> None:
        # Settle before the first sweep: on boot the fleet_overview tail reads
        # panes that are still being rehydrated by the snapshot worker, and a
        # shell that merely looks idle for a moment is not paused.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=120)
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("nudge sweep failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ tick

    async def tick_once(self) -> dict:
        """One sweep. Returns a summary; separated from the loop for tests and
        for a manual kick."""
        from aria.api.routes.shell_nudge import nudge_shell_once, safety_engaged

        summary: dict[str, Any] = {
            "checked_at": self._now(),
            "blocked": 0,
            "nudged": 0,
            "skipped": 0,
            "escalated": 0,
            "reasons": {},
        }
        # Checked once for the whole sweep as well as per shell inside the
        # shared path: an engaged stop should cost one gate read, not one per
        # shell, and the sweep should say why it did nothing.
        if await safety_engaged(self.db):
            summary["reason"] = "stop_engaged"
            self._last = summary
            return summary

        shell_service = await self._resolve_shell_service()
        notifier = self._resolve_notifier()
        if shell_service is None:
            summary["reason"] = "no_shell_service"
            self._last = summary
            return summary
        if notifier is None:
            # Refuse rather than sweep half-armed: the third strike IS the
            # feature, and nudging with no way to raise it would burn the
            # attempts counter and lose the escalation silently.
            summary["reason"] = "no_notifier"
            self._last = summary
            return summary

        try:
            overview = await shell_service.fleet_overview()
        except Exception as exc:
            logger.warning("nudge sweep: fleet_overview failed: %s", exc)
            summary["reason"] = "fleet_overview_failed"
            self._last = summary
            return summary

        for row in overview:
            if not (row.get("activity_state") == "blocked" or row.get("awaiting_input")):
                continue
            summary["blocked"] += 1
            name = row.get("name")
            if not name:
                continue
            try:
                result = await nudge_shell_once(
                    name,
                    db=self.db,
                    shell_service=shell_service,
                    notifier=notifier,
                )
            except Exception as exc:
                # One bad shell must not end the sweep — the fleet's other
                # blocked sessions are still waiting on their nudge.
                logger.warning("nudge of %s failed: %s", name, exc)
                summary["skipped"] += 1
                summary["reasons"]["error"] = summary["reasons"].get("error", 0) + 1
                continue
            if result.get("nudged"):
                summary["nudged"] += 1
                if result.get("escalated"):
                    summary["escalated"] += 1
            else:
                summary["skipped"] += 1
                reason = result.get("reason") or "unknown"
                summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
            if self._stop.is_set():
                break

        summary["reason"] = "ok"
        self._last = summary
        logger.info(
            "nudge sweep: %d blocked, %d nudged, %d skipped, %d escalated",
            summary["blocked"], summary["nudged"], summary["skipped"],
            summary["escalated"],
        )
        return summary

    # --------------------------------------------------------------- plumbing

    async def _resolve_shell_service(self):
        if self._shell_service is not None:
            return self._shell_service
        try:
            from aria.api.deps import resolve_shell_service

            self._shell_service = await resolve_shell_service(self.db)
        except Exception as exc:
            logger.warning("nudge worker: shell service unavailable: %s", exc)
            self._shell_service = None
        return self._shell_service

    def _resolve_notifier(self):
        if self._notifier is not None:
            return self._notifier
        try:
            from aria.api.deps import get_notification_service

            self._notifier = get_notification_service()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("nudge worker: notifier unavailable: %s", exc)
            self._notifier = None
        return self._notifier
