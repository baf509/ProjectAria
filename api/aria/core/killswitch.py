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

    @property
    def is_active(self) -> bool:
        return self._active

    def set_db(self, db: "AsyncIOMotorDatabase") -> None:
        self._db = db

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
    ) -> dict:
        """Activate the killswitch, cancelling all running autonomous work."""
        self._active = True
        self._activated_at = datetime.now(timezone.utc)
        self._reason = reason
        await self._persist()

        cancelled_tasks = 0
        if task_runner is not None:
            cancelled_tasks = await self._cancel_all_tasks(task_runner)

        if notification_service is not None:
            try:
                await notification_service.notify(
                    source="killswitch",
                    event_type="activated",
                    detail=f"Killswitch activated: {reason}. Cancelled {cancelled_tasks} task(s).",
                    cooldown_seconds=0,
                )
            except Exception as exc:
                logger.warning("Failed to send killswitch notification: %s", exc)

        logger.warning(
            "Killswitch ACTIVATED: %s (cancelled %d tasks)", reason, cancelled_tasks
        )
        return {
            "active": True,
            "reason": reason,
            "activated_at": self._activated_at,
            "cancelled_tasks": cancelled_tasks,
        }

    async def _cancel_all_tasks(self, task_runner) -> int:
        """Cancel all running tasks via the task runner."""
        return await task_runner.cancel_all()

    async def deactivate(self) -> dict:
        """Deactivate the killswitch, allowing operations to resume."""
        self._active = False
        self._reason = None
        self._activated_at = None
        await self._persist()
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
