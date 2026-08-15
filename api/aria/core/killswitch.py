"""
ARIA - Killswitch / Emergency Stop

Purpose: Global safety killswitch that halts all autonomous operations.

Related Spec Sections:
- Safety: Emergency stop for tasks, autopilot, coding sessions
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


class Killswitch:
    """Global emergency stop that cancels all autonomous operations."""

    def __init__(self):
        self._active: bool = False
        self._activated_at: Optional[datetime] = None
        self._reason: Optional[str] = None
        self._db: Optional[AsyncIOMotorDatabase] = None
        self._escalation_manager = None
        self._escalation_id: Optional[str] = None
        self._coding_manager = None

    @property
    def is_active(self) -> bool:
        return self._active

    def set_db(self, db: "AsyncIOMotorDatabase") -> None:
        self._db = db

    def set_coding_manager(self, manager) -> None:
        """Wire the coding-session manager so activation can stop sessions that
        are ALREADY RUNNING. Until 2026-08-15 the killswitch only refused new
        spawns: the thing you press the button about kept going."""
        self._coding_manager = manager

    async def load_state(self, db: "AsyncIOMotorDatabase") -> None:
        """Load persisted killswitch state on startup."""
        self._db = db
        doc = await db.killswitch.find_one({"_id": "global"})
        if doc and doc.get("active"):
            self._active = True
            self._activated_at = doc.get("activated_at")
            self._reason = doc.get("reason")
            logger.warning("Killswitch is ACTIVE (persisted state): %s", self._reason)

    async def _persist(self) -> None:
        if self._db is None:
            return
        await self._db.killswitch.update_one(
            {"_id": "global"},
            {
                "$set": {
                    "active": self._active,
                    "activated_at": self._activated_at,
                    "reason": self._reason,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    async def activate(
        self,
        reason: str = "Manual activation",
        *,
        task_runner=None,
        notification_service=None,
        escalation_manager=None,
        coding_manager=None,
    ) -> dict:
        """Activate the killswitch, cancelling all running autonomous work."""
        self._active = True
        self._activated_at = datetime.now(timezone.utc)
        self._reason = reason
        await self._persist()

        cancelled_tasks = 0
        if task_runner is not None:
            cancelled_tasks = await self._cancel_all_tasks(task_runner)

        stopped_sessions = await self._stop_running_sessions(coding_manager, reason)

        if notification_service is not None:
            try:
                await notification_service.notify(
                    source="killswitch",
                    event_type="activated",
                    detail=(
                        f"Killswitch activated: {reason}. Cancelled {cancelled_tasks} task(s), "
                        f"stopped {stopped_sessions} coding session(s)."
                    ),
                    cooldown_seconds=0,
                )
            except Exception as exc:
                logger.warning("Failed to send killswitch notification: %s", exc)

        if escalation_manager is not None:
            try:
                from aria.notifications.escalation import Severity
                esc = await escalation_manager.escalate(
                    source="killswitch",
                    severity=Severity.CRITICAL,
                    description=f"Killswitch activated: {reason}",
                    metadata={"cancelled_tasks": cancelled_tasks},
                )
                self._escalation_manager = escalation_manager
                self._escalation_id = esc.escalation_id
            except Exception as exc:
                logger.warning("Failed to create killswitch escalation: %s", exc)

        logger.warning(
            "Killswitch ACTIVATED: %s (cancelled %d tasks, stopped %d coding sessions)",
            reason, cancelled_tasks, stopped_sessions,
        )
        return {
            "active": True,
            "reason": reason,
            "activated_at": self._activated_at,
            "cancelled_tasks": cancelled_tasks,
            "stopped_sessions": stopped_sessions,
        }

    async def _cancel_all_tasks(self, task_runner) -> int:
        """Cancel all running tasks via the task runner."""
        return await task_runner.cancel_all()

    async def _stop_running_sessions(self, coding_manager, reason: str) -> int:
        """Kill every running coding session (proposal §7.3).

        Best-effort by construction: the killswitch's first job is to be
        activatable, so a manager that isn't wired, a Mongo blip or one wedged
        tmux session must not make pressing the button fail. The state is
        already persisted by the time this runs, so new spawns are blocked
        either way.
        """
        manager = coding_manager or self._coding_manager
        if manager is None:
            from aria.agents.session import resolve_active_session_manager

            manager = resolve_active_session_manager()
        if manager is None:
            return 0
        try:
            result = await manager.stop_all_running(reason=f"killswitch: {reason}")
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.error("Killswitch could not stop running coding sessions: %s", exc)
            return 0
        return int((result or {}).get("stopped") or 0)

    async def deactivate(self) -> dict:
        """Deactivate the killswitch, allowing operations to resume."""
        self._active = False
        self._reason = None
        self._activated_at = None
        await self._persist()
        if self._escalation_id and self._escalation_manager is not None:
            try:
                await self._escalation_manager.resolve(
                    self._escalation_id, "Killswitch deactivated"
                )
            except Exception as exc:
                logger.warning("Failed to resolve killswitch escalation: %s", exc)
            self._escalation_id = None
        logger.info("Killswitch deactivated")
        return {"active": False}

    def status(self) -> dict:
        return {
            "active": self._active,
            "reason": self._reason,
            "activated_at": self._activated_at,
        }

    def check_or_raise(self, operation: str = "operation") -> None:
        """Raise RuntimeError if killswitch is active. Call before autonomous work."""
        if self._active:
            raise RuntimeError(
                f"Killswitch is active — {operation} blocked. "
                f"Reason: {self._reason}"
            )
