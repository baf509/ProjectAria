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
from aria.agents.mail import AgentMailbox, MessageType
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
        self.mailbox = AgentMailbox(db)
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
            try:
                await self._drain_orchestrator_mail()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Watchdog mail drain error: %s", e, exc_info=True)
            await asyncio.sleep(settings.coding_watchdog_interval_seconds)

    async def _drain_orchestrator_mail(self) -> None:
        """Consume inter-agent mail addressed to the orchestrator and surface it.

        Sub-agents send TASK_DONE / ERROR / HANDOFF messages to
        recipient="orchestrator" (see agents/session.py). Nothing else reads
        that mailbox, so the watchdog drains it, notifies the user, and marks
        each message read so it isn't re-processed.
        """
        unread = await self.mailbox.get_unread("orchestrator", limit=20)
        for msg in unread:
            try:
                if msg.msg_type == MessageType.TASK_DONE:
                    exit_status = (msg.metadata or {}).get("exit_status", "completed")
                    detail = f"Sub-agent {msg.sender} finished ({exit_status}): {msg.body[:200]}"
                    event_type = "agent_task_done"
                elif msg.msg_type == MessageType.ERROR:
                    detail = f"Sub-agent {msg.sender} reported an error: {msg.body[:200]}"
                    event_type = "agent_error"
                elif msg.msg_type == MessageType.HANDOFF:
                    detail = f"Sub-agent {msg.sender} handed off work: {msg.subject}"
                    event_type = "agent_handoff"
                else:
                    detail = f"Mail from {msg.sender}: {msg.subject}"
                    event_type = "agent_mail"
                try:
                    await self.notification_service.notify(
                        source="agents",
                        event_type=event_type,
                        detail=detail,
                        cooldown_seconds=0,
                    )
                except Exception as e:
                    logger.warning("Failed to notify for agent mail: %s", e)
                await self.mailbox.mark_read(msg.message_id)
            except Exception as e:
                logger.error(
                    "Failed to process agent mail %s: %s",
                    getattr(msg, "message_id", "?"), e,
                )

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
                        # ARIA can't query the Claude subscription quota — there
                        # is no API for it. Pane output is the only signal, so
                        # record a cooldown here and let the complexity router
                        # demote new sessions to the fallback tier until it
                        # expires. Only for Claude-backed sessions: a rate limit
                        # on Fireworks says nothing about the subscription.
                        if session.get("backend") == "claude_code":
                            await self._record_quota_cooldown(session_id, output)
                    elif reason == StuckReason.RETRY_LOOP:
                        detail = "Agent stuck in retry loop"
                    elif reason == StuckReason.WAITING_INPUT:
                        detail = "Agent waiting for interactive input"
                    else:
                        preview = "\n".join(output.splitlines()[-3:])
                        detail = preview or "No output"

                    # A looping session is *expected* to sit idle at its prompt —
                    # the nudge handles it, so skip the generic stall alert.
                    if not session.get("loop_config"):
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

            # Ralph loop: nudge a looping session forward when it goes idle.
            if session.get("loop_config"):
                try:
                    await self._maybe_nudge(session, state)
                except Exception as e:
                    logger.error("Loop nudge error for %s: %s", session_id, e, exc_info=True)

            if settings.coding_auto_respond_prompts:
                await self._auto_respond(session_id, output)

        # Prune tracking state for sessions that left `running` since the last
        # tick (completed/stopped/failed) -- list_sessions(status="running")
        # above no longer returns them, so nothing else ever removes their
        # entry and this dict would otherwise grow for the process lifetime.
        running_ids = {str(session["_id"]) for session in sessions}
        for stale_id in set(self._session_state) - running_ids:
            self._session_state.pop(stale_id, None)

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

    async def _record_quota_cooldown(self, session_id: str, output: str) -> None:
        """Mark the Claude subscription as cooling down so the complexity router
        routes new sessions to the fallback tier. Advisory — never fatal."""
        try:
            from aria.agents.routing import record_quota_exhaustion

            preview = "\n".join(output.splitlines()[-3:])[:200]
            await record_quota_exhaustion(
                self.session_manager.db,
                reason=f"coding:{session_id} rate-limited — {preview}",
            )
        except Exception as exc:
            logger.debug("quota cooldown record failed for %s: %s", session_id, exc)

    async def _auto_respond(self, session_id: str, output: str) -> None:
        for pattern in SAFE_PROMPTS:
            if pattern.search(output):
                await self.session_manager.send_input(session_id, "")
                return
        for pattern in NORMAL_PROMPTS:
            if pattern.search(output):
                await self.session_manager.send_input(session_id, "y")
                return

    # ----- Ralph loop: keep a session going, nudge it forward when idle -----

    @staticmethod
    def _loop_nudge_text(loop: dict) -> str:
        """The instruction to feed on each nudge. A nudge_prompt_file is re-read
        fresh every time, so you can edit it to steer a live session."""
        path = loop.get("nudge_prompt_file")
        if path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read().strip()
                if text:
                    return text
            except OSError as e:
                logger.debug("loop nudge_prompt_file unreadable (%s): %s", path, e)
        return loop.get("nudge_prompt") or settings.coding_loop_nudge_prompt

    async def _end_loop(self, session_id: str, reason: str, *, stop: bool) -> None:
        """Clear loop_config so nudging stops; optionally stop the session too.
        The workspace/git state is the durable record, so stopping is safe."""
        await self.db.coding_sessions.update_one(
            {"_id": session_id},
            {"$set": {"loop_config": None, "updated_at": datetime.now(timezone.utc)}},
        )
        logger.info("Ralph loop ended for %s: %s", session_id, reason)
        try:
            await self.notification_service.notify(
                source=f"coding:{session_id}",
                event_type="loop:ended",
                detail=f"Ralph loop ended: {reason}",
                cooldown_seconds=30,
            )
        except Exception:
            pass
        if stop:
            try:
                await self.session_manager.stop_session(session_id)
            except Exception as e:
                logger.debug("loop-end stop_session failed for %s: %s", session_id, e)

    async def _maybe_nudge(self, session: dict, state: dict) -> None:
        loop = session.get("loop_config") or {}
        session_id = str(session["_id"])
        now = datetime.now(timezone.utc)
        output = state.get("last_output") or ""

        # 1) Safety leash — re-checked on EVERY tick, not just at launch.
        from aria.api.deps import get_killswitch, resolve_estop_manager
        try:
            get_killswitch().check_or_raise("ralph loop nudge")
        except Exception:
            await self._end_loop(session_id, "killswitch engaged", stop=False)
            return
        try:
            estop = await resolve_estop_manager(self.db)
            if await estop.is_active():
                await self._end_loop(session_id, "emergency stop active", stop=False)
                return
        except Exception as e:  # fail closed — stop nudging if we can't verify
            await self._end_loop(session_id, f"e-stop check failed: {e}", stop=False)
            return

        # 2) Done? (done token seen in the visible output)
        done_regex = loop.get("done_regex")
        if done_regex:
            try:
                if re.search(done_regex, output):
                    await self._end_loop(session_id, "done signal seen", stop=True)
                    return
            except re.error:
                pass

        # 3) Wall-clock deadline.
        started = session.get("loop_started_at")
        deadline_minutes = int(loop.get("deadline_minutes") or 0)
        if started and deadline_minutes and now - started >= timedelta(minutes=deadline_minutes):
            await self._end_loop(session_id, f"deadline reached ({deadline_minutes}m)", stop=True)
            return

        # 4) Nudge cap.
        nudges = int(session.get("loop_nudges", 0))
        max_nudges = int(loop.get("max_nudges") or 0)
        if max_nudges and nudges >= max_nudges:
            await self._end_loop(session_id, f"max nudges reached ({max_nudges})", stop=True)
            return

        # 5) Idle long enough at the prompt? (debounced so we don't double-nudge)
        idle_seconds = int(loop.get("idle_seconds") or settings.coding_loop_idle_seconds)
        idle_for = (now - state.get("last_changed_at", now)).total_seconds()
        if idle_for < idle_seconds:
            return
        last_nudge = session.get("last_nudge_at")
        if last_nudge and (now - last_nudge).total_seconds() < idle_seconds:
            return

        # 6) Nudge it forward.
        nudge_text = self._loop_nudge_text(loop)
        await self.session_manager.send_input(session_id, nudge_text)
        nudges += 1
        await self.db.coding_sessions.update_one(
            {"_id": session_id},
            {"$set": {"last_nudge_at": now, "updated_at": now, "loop_nudges": nudges}},
        )
        # Reset the idle clock so the next nudge waits for a fresh stall.
        state["last_changed_at"] = now
        logger.info("Ralph loop nudged session %s (nudge #%d)", session_id, nudges)

        notify_every = int(loop.get("notify_every") or 0)
        if notify_every and nudges % notify_every == 0:
            try:
                await self.notification_service.notify(
                    source=f"coding:{session_id}",
                    event_type="loop:nudge",
                    detail=f"Ralph loop still running — {nudges} nudges so far",
                    cooldown_seconds=0,
                )
            except Exception:
                pass
