"""
ARIA - Coding Session Watchdog

Purpose: Monitor running coding sessions for stalls and interactive prompts.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.notifications.service import NotificationService
from aria.agents.review import CodingReviewService
from aria.agents.session import CodingSessionManager

SAFE_PROMPTS = [
    re.compile(r"\bpress enter\b", re.IGNORECASE),
    re.compile(r"\bcontinue\?\b", re.IGNORECASE),
]
NORMAL_PROMPTS = [
    re.compile(r"\b[yY]/[nN]\b"),
    re.compile(r"\bproceed\?\b", re.IGNORECASE),
]


class CodingWatchdog:
    """Background watchdog for coding sessions."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        session_manager: CodingSessionManager,
        notification_service: NotificationService,
        review_service: CodingReviewService | None = None,
    ):
        self.db = db
        self.session_manager = session_manager
        self.notification_service = notification_service
        self.review_service = review_service
        self._task: asyncio.Task | None = None
        self._session_state: dict[str, dict] = {}

    async def start(self) -> dict:
        if self._task is not None and not self._task.done():
            return {"running": True}
        self._task = asyncio.create_task(self._loop())
        return {"running": True}

    async def stop(self) -> dict:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        return {"running": False}

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "tracked_sessions": len(self._session_state),
        }

    async def set_deadline(self, session_id: str, minutes: int) -> None:
        self._session_state.setdefault(session_id, {})["deadline_at"] = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    async def _loop(self) -> None:
        while True:
            await self._check_sessions()
            await asyncio.sleep(settings.coding_watchdog_interval_seconds)

    async def _check_sessions(self) -> None:
        sessions = await self.session_manager.list_sessions(status="running")
        for session in sessions:
            session_id = str(session["_id"])
            output = self.session_manager.get_output(session_id, lines=100)
            output_hash = hashlib.md5(output.encode("utf-8")).hexdigest()
            state = self._session_state.setdefault(
                session_id,
                {"last_hash": None, "last_changed_at": datetime.now(timezone.utc)},
            )

            if output_hash != state.get("last_hash"):
                state["last_hash"] = output_hash
                state["last_changed_at"] = datetime.now(timezone.utc)
            elif datetime.now(timezone.utc) - state["last_changed_at"] >= timedelta(seconds=settings.coding_stall_seconds):
                preview = "\n".join(output.splitlines()[-3:])
                await self.notification_service.notify(
                    source=f"coding:{session_id}",
                    event_type="stalled",
                    detail=preview or "No output",
                    cooldown_seconds=60,
                )

            deadline_at = state.get("deadline_at")
            if deadline_at and datetime.now(timezone.utc) >= deadline_at:
                await self.session_manager.stop_session(session_id)
                await self.notification_service.notify(
                    source=f"coding:{session_id}",
                    event_type="deadline",
                    detail="Session stopped due to deadline",
                    cooldown_seconds=60,
                )

            if settings.coding_auto_respond_prompts:
                await self._auto_respond(session_id, output)

        if self.review_service:
            completed_sessions = await self.db.coding_sessions.find(
                {"status": {"$in": ["completed", "failed"]}}
            ).to_list(length=100)
            for session in completed_sessions:
                session_id = str(session["_id"])
                existing_report = await self.review_service.get_report(session_id)
                if existing_report:
                    continue
                try:
                    await self.review_service.review_session(session_id)
                except Exception:
                    # Review is best-effort; keep watchdog alive.
                    pass

    async def _auto_respond(self, session_id: str, output: str) -> None:
        for pattern in SAFE_PROMPTS:
            if pattern.search(output):
                await self.session_manager.send_input(session_id, "")
                return
        for pattern in NORMAL_PROMPTS:
            if pattern.search(output):
                await self.session_manager.send_input(session_id, "y")
                return
