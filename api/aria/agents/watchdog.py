"""
ARIA - Coding Session Watchdog

Purpose: Monitor running coding sessions for stalls, interactive prompts,
and content-aware stuck patterns. Inspired by Gas Town's stuck-agent-dog.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from enum import Enum

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.agents.budget_guard import BudgetLevel, ContextBudgetGuard
from aria.agents.checkpoint import write_checkpoint
from aria.config import settings
from aria.notifications.service import NotificationService
from aria.agents.review import CodingReviewService
from aria.agents.session import CodingSessionManager

logger = logging.getLogger(__name__)

SAFE_PROMPTS = [
    re.compile(r"\bpress enter\b", re.IGNORECASE),
    re.compile(r"\bcontinue\?\b", re.IGNORECASE),
]
NORMAL_PROMPTS = [
    re.compile(r"\b[yY]/[nN]\b"),
    re.compile(r"\bproceed\?\b", re.IGNORECASE),
]


class StuckReason(str, Enum):
    """Why an agent appears stuck — drives what action to take."""
    IDLE = "idle"                    # No output change (original hash check)
    RETRY_LOOP = "retry_loop"       # Repeating the same error/action
    RATE_LIMITED = "rate_limited"    # Hit API rate limits
    CONTEXT_FULL = "context_full"   # Context window exhaustion signals
    WAITING_INPUT = "waiting_input" # Waiting for interactive input
    UNKNOWN = "unknown"


# Patterns that indicate specific stuck states
_RETRY_PATTERNS = [
    re.compile(r"(error|failed|exception).*\b(retry|retrying|attempt)\b", re.IGNORECASE),
    re.compile(r"(attempt|try)\s+\d+\s*(of|/)\s*\d+", re.IGNORECASE),
]
_RATE_LIMIT_PATTERNS = [
    re.compile(r"\b(429|rate.?limit|too many requests|throttl)\b", re.IGNORECASE),
    re.compile(r"\b(overloaded|capacity|quota)\b", re.IGNORECASE),
]
_CONTEXT_PATTERNS = [
    # NOTE: Context exhaustion is primarily handled by ContextBudgetGuard
    # (budget_guard.py) which has more granular thresholds (WARN/SOFT/HARD).
    # These patterns are kept only for the stuck-diagnosis path — when context
    # fills up the agent also appears "stuck" (no new output). The budget guard
    # handles notifications and checkpointing; this just tags the reason.
    re.compile(r"\b(context.?(window|length|limit)|max.?tokens|token.?limit)\b", re.IGNORECASE),
    re.compile(r"\b(conversation.?too.?long|input.?too.?large)\b", re.IGNORECASE),
]
_INPUT_PATTERNS = [
    re.compile(r"\b(enter|type|input)\b.*[?:]\s*$", re.IGNORECASE),
    re.compile(r"^>\s*$", re.MULTILINE),
    re.compile(r"\$\s*$"),
]


def diagnose_stuck(output: str, previous_output: str | None = None) -> StuckReason:
    """Analyze agent output to determine why it appears stuck.

    Inspects the last ~30 lines of output for known stuck patterns.
    """
    if not output:
        return StuckReason.IDLE

    tail = "\n".join(output.splitlines()[-30:])

    # Check for rate limiting first (most urgent)
    for pattern in _RATE_LIMIT_PATTERNS:
        if pattern.search(tail):
            return StuckReason.RATE_LIMITED

    # Check for context exhaustion
    for pattern in _CONTEXT_PATTERNS:
        if pattern.search(tail):
            return StuckReason.CONTEXT_FULL

    # Check for retry loops — look for repeated error lines
    lines = tail.splitlines()
    if len(lines) >= 6:
        # Check if the last N lines are repeating a pattern
        last_3 = "\n".join(lines[-3:]).strip()
        prev_3 = "\n".join(lines[-6:-3]).strip()
        if last_3 and last_3 == prev_3:
            return StuckReason.RETRY_LOOP

    for pattern in _RETRY_PATTERNS:
        if pattern.search(tail):
            return StuckReason.RETRY_LOOP

    # Check for waiting on input
    for pattern in _INPUT_PATTERNS:
        if pattern.search(tail):
            return StuckReason.WAITING_INPUT

    # Fallback: if output hasn't changed, it's just idle
    if previous_output and output.strip() == previous_output.strip():
        return StuckReason.IDLE

    return StuckReason.UNKNOWN


class CodingWatchdog:
    """Background watchdog for coding sessions with content-aware stuck detection."""

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
        self.budget_guard = ContextBudgetGuard()
        self._task: asyncio.Task | None = None
        self._session_state: dict[str, dict] = {}

    async def start(self) -> dict:
        if self._task is not None and not self._task.done():
            return {"running": True}
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Coding watchdog started (interval=%ds, stall=%ds)",
            settings.coding_watchdog_interval_seconds,
            settings.coding_stall_seconds,
        )
        return {"running": True}

    async def stop(self) -> dict:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        logger.info("Coding watchdog stopped")
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
            try:
                await self._check_sessions()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Watchdog check error: %s", e, exc_info=True)
            await asyncio.sleep(settings.coding_watchdog_interval_seconds)

    async def _check_sessions(self) -> None:
        sessions = await self.session_manager.list_sessions(status="running")
        for session in sessions:
            session_id = str(session["_id"])
            output = await self.session_manager.get_output(session_id, lines=100)
            output_hash = hashlib.md5(output.encode("utf-8")).hexdigest()
            is_new = session_id not in self._session_state
            state = self._session_state.setdefault(
                session_id,
                {
                    "last_hash": None,
                    "last_changed_at": datetime.now(timezone.utc),
                    "last_output": None,
                    "stuck_reason": None,
                },
            )
            # For newly tracked sessions, initialize hash from current output
            # to avoid falsely treating pre-existing output as a stall
            if is_new:
                state["last_hash"] = output_hash
                state["last_output"] = output
                continue

            if output_hash != state.get("last_hash"):
                state["last_hash"] = output_hash
                state["last_changed_at"] = datetime.now(timezone.utc)
                state["last_output"] = output
                state["stuck_reason"] = None
            elif datetime.now(timezone.utc) - state["last_changed_at"] >= timedelta(seconds=settings.coding_stall_seconds):
                # Content-aware diagnosis
                reason = diagnose_stuck(output, state.get("last_output"))
                state["stuck_reason"] = reason.value
                logger.warning(
                    "Session %s stuck: %s (no output change for %ds)",
                    session_id, reason.value, settings.coding_stall_seconds,
                )

                # Severity and action depends on the reason.
                # NOTE: CONTEXT_FULL checkpointing and notifications are handled
                # by the ContextBudgetGuard below — we only tag the reason here
                # and skip duplicate notification for that case.
                if reason == StuckReason.CONTEXT_FULL:
                    # Budget guard handles checkpoint + notification for context issues
                    pass
                else:
                    if reason == StuckReason.RATE_LIMITED:
                        detail = "Agent hit API rate limits — pausing may help"
                    elif reason == StuckReason.RETRY_LOOP:
                        detail = "Agent stuck in retry loop"
                    elif reason == StuckReason.WAITING_INPUT:
                        detail = "Agent waiting for interactive input"
                    else:
                        preview = "\n".join(output.splitlines()[-3:])
                        detail = preview or "No output"

                    await self.notification_service.notify(
                        source=f"coding:{session_id}",
                        event_type=f"stalled:{reason.value}",
                        detail=detail,
                        cooldown_seconds=60,
                    )

            deadline_at = state.get("deadline_at")
            if deadline_at and datetime.now(timezone.utc) >= deadline_at:
                logger.info("Session %s reached deadline, stopping", session_id)
                await self.session_manager.stop_session(session_id)
                await self.notification_service.notify(
                    source=f"coding:{session_id}",
                    event_type="deadline",
                    detail="Session stopped due to deadline",
                    cooldown_seconds=60,
                )

            # Context budget guard
            budget_level = self.budget_guard.check(session_id, output)
            if budget_level is not None:
                if budget_level == BudgetLevel.HARD_GATE:
                    # Critical: checkpoint and stop
                    try:
                        await write_checkpoint(
                            self.db, session_id, session["workspace"],
                            notes="Context budget hard gate — session will be stopped",
                        )
                    except Exception as e:
                        logger.warning("Failed to write hard-gate checkpoint for %s: %s", session_id, e)
                    await self.session_manager.stop_session(session_id)
                    await self.notification_service.notify(
                        source=f"coding:{session_id}",
                        event_type="budget:hard_gate",
                        detail="Context window exhausted — session checkpointed and stopped. Use resume to continue.",
                        cooldown_seconds=60,
                    )
                elif budget_level == BudgetLevel.SOFT_GATE:
                    # Checkpoint and warn
                    try:
                        await write_checkpoint(
                            self.db, session_id, session["workspace"],
                            notes="Context budget soft gate — approaching limit",
                        )
                    except Exception as e:
                        logger.warning("Failed to write soft-gate checkpoint for %s: %s", session_id, e)
                    await self.notification_service.notify(
                        source=f"coding:{session_id}",
                        event_type="budget:soft_gate",
                        detail="Context window nearing limit — checkpoint written",
                        cooldown_seconds=120,
                    )
                elif budget_level == BudgetLevel.WARN:
                    await self.notification_service.notify(
                        source=f"coding:{session_id}",
                        event_type="budget:warn",
                        detail="Context window getting large",
                        cooldown_seconds=300,
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
                except Exception as e:
                    logger.debug("Auto-review failed for %s: %s", session_id, e)

    async def _auto_respond(self, session_id: str, output: str) -> None:
        for pattern in SAFE_PROMPTS:
            if pattern.search(output):
                await self.session_manager.send_input(session_id, "")
                return
        for pattern in NORMAL_PROMPTS:
            if pattern.search(output):
                await self.session_manager.send_input(session_id, "y")
                return
