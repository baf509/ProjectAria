"""
ARIA - Scheduler Service

Phase: 14
Purpose: Scheduled tasks and reminders with persistent MongoDB-backed schedules.

Related Spec Sections:
- Section 14: Scheduled Tasks & Reminders
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.notifications.service import NotificationService
from aria.tasks.runner import TaskRunner

logger = logging.getLogger(__name__)

# Day name to weekday number (Monday=0, Sunday=6)
_DAY_MAP = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


class SchedulerService:
    """Tick-based scheduler that checks for due schedules every 15 seconds."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        task_runner: TaskRunner,
        notification_service: NotificationService,
        orchestrator_factory=None,
    ):
        self.db = db
        self.task_runner = task_runner
        self.notification_service = notification_service
        self.orchestrator_factory = orchestrator_factory
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the scheduler tick loop."""
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("Scheduler started")

    async def stop(self):
        """Stop the scheduler tick loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Scheduler stopped")

    async def _tick_loop(self):
        """Check for due schedules every 15 seconds."""
        _ticks = 0
        _executed = 0
        while self._running:
            try:
                count = await self._check_due_schedules()
                _executed += count
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Scheduler tick error: {e}")
            _ticks += 1
            # Log a summary every ~30 minutes (120 ticks * 15s)
            if _ticks % 120 == 0:
                total_enabled = await self.db.schedules.count_documents({"enabled": True})
                logger.info(
                    "Scheduler status: %d enabled schedule(s), %d executed in last 30m",
                    total_enabled, _executed,
                )
                _executed = 0
            await asyncio.sleep(15)

    async def _check_due_schedules(self) -> int:
        """Find and execute schedules that are due. Returns count executed."""
        now = datetime.now(timezone.utc)
        due = await self.db.schedules.find({
            "enabled": True,
            "next_run_at": {"$lte": now},
        }).to_list(length=50)

        executed = 0
        for schedule in due:
            try:
                await self._execute_schedule(schedule)
                executed += 1
            except Exception as e:
                logger.error(
                    f"Failed to execute schedule {schedule.get('name', schedule['_id'])}: {e}"
                )
        return executed

    async def _execute_schedule(self, schedule: dict):
        """Execute a single schedule and update next_run_at."""
        action = schedule["action"]
        params = schedule.get("params", {})

        if action == "remind":
            await self.notification_service.notify(
                source="scheduler",
                event_type="reminder",
                detail=params.get("message", "Reminder"),
                cooldown_seconds=0,
            )
        elif action == "prompt":
            # Submit a message through the orchestrator as a background task
            if self.orchestrator_factory:
                message = params.get("message", "")
                conversation_id = params.get("conversation_id")

                async def _run_prompt():
                    orchestrator = await self.orchestrator_factory()
                    chunks = []
                    async for chunk in orchestrator.chat(
                        message=message,
                        conversation_id=conversation_id,
                    ):
                        chunks.append(chunk)
                    return {"chunks": len(chunks)}

                await self.task_runner.submit_task(
                    name=f"scheduled-prompt:{schedule.get('name', '')}",
                    coroutine_factory=_run_prompt,
                    notify=True,
                    metadata={"task_kind": "scheduled_prompt", "schedule_id": str(schedule["_id"])},
                )
        elif action == "tool":
            tool_name = params.get("tool_name")
            tool_args = params.get("tool_args", {})
            if tool_name:
                async def _run_tool():
                    from aria.api.deps import get_tool_router
                    router = get_tool_router()
                    result = await router.execute_tool(tool_name, tool_args)
                    return result

                await self.task_runner.submit_task(
                    name=f"scheduled-tool:{tool_name}",
                    coroutine_factory=_run_tool,
                    notify=True,
                    metadata={"task_kind": "scheduled_tool", "schedule_id": str(schedule["_id"])},
                )
        elif action == "notify":
            await self.notification_service.notify(
                source="scheduler",
                event_type=params.get("event_type", "scheduled"),
                detail=params.get("detail", "Scheduled notification"),
                cooldown_seconds=0,
            )

        # Update schedule based on type
        now = datetime.now(timezone.utc)
        if schedule["schedule_type"] == "once":
            await self.db.schedules.update_one(
                {"_id": schedule["_id"]},
                {"$set": {
                    "enabled": False,
                    "last_run_at": now,
                    "run_count": schedule.get("run_count", 0) + 1,
                    "updated_at": now,
                }},
            )
        elif schedule["schedule_type"] == "recurring":
            next_run = self._compute_next_run(schedule.get("cron_expr") or "")
            await self.db.schedules.update_one(
                {"_id": schedule["_id"]},
                {"$set": {
                    "next_run_at": next_run,
                    "last_run_at": now,
                    "run_count": schedule.get("run_count", 0) + 1,
                    "updated_at": now,
                }},
            )

        logger.info(
            f"Executed schedule '{schedule.get('name', schedule['_id'])}' "
            f"(action={action}, type={schedule['schedule_type']})"
        )

    @staticmethod
    def _compute_next_run(cron_expr: str) -> datetime:
        """Compute the next run time from a simplified cron expression.

        Supported formats:
            "every Xm"          - every X minutes
            "every Xh"          - every X hours
            "hourly"            - every hour on the hour
            "daily HH:MM"       - daily at HH:MM UTC
            "weekly DAY HH:MM"  - weekly on DAY at HH:MM UTC
        """
        now = datetime.now(timezone.utc)
        expr = cron_expr.strip().lower()

        # "every Xm"
        match = re.match(r"every\s+(\d+)\s*m(?:in(?:ute)?s?)?$", expr)
        if match:
            minutes = int(match.group(1))
            if minutes < 1:
                minutes = 1
            return now + timedelta(minutes=minutes)

        # "every Xh"
        match = re.match(r"every\s+(\d+)\s*h(?:(?:ou)?rs?)?$", expr)
        if match:
            hours = int(match.group(1))
            if hours < 1:
                hours = 1
            return now + timedelta(hours=hours)

        # "hourly"
        if expr == "hourly":
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            return next_hour

        # "daily HH:MM"
        match = re.match(r"daily\s+(\d{1,2}):(\d{2})$", expr)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate

        # "weekly DAY HH:MM"
        match = re.match(r"weekly\s+(\w+)\s+(\d{1,2}):(\d{2})$", expr)
        if match:
            day_name = match.group(1).lower()
            hour = int(match.group(2))
            minute = int(match.group(3))
            target_weekday = _DAY_MAP.get(day_name)
            if target_weekday is None:
                raise ValueError(f"Unknown day name: {day_name}")
            days_ahead = (target_weekday - now.weekday()) % 7
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
            if candidate <= now:
                candidate += timedelta(weeks=1)
            return candidate

        raise ValueError(
            f"Unsupported cron expression: '{cron_expr}'. "
            "Supported: 'every Xm', 'every Xh', 'hourly', 'daily HH:MM', 'weekly DAY HH:MM'"
        )

    async def create_schedule(
        self,
        name: str,
        schedule_type: str,
        action: str,
        params: dict,
        cron_expr: Optional[str] = None,
        run_at: Optional[datetime] = None,
    ) -> str:
        """Create a new schedule or one-shot reminder.

        Args:
            name: Human-readable name for the schedule.
            schedule_type: "once" or "recurring".
            action: "remind", "prompt", "tool", or "notify".
            params: Action-specific parameters.
            cron_expr: Simplified cron expression (required for recurring).
            run_at: When to run (required for one-shot).

        Returns:
            The ID of the created schedule document.
        """
        now = datetime.now(timezone.utc)

        if schedule_type == "once":
            if run_at is None:
                raise ValueError("run_at is required for one-shot schedules")
            next_run = run_at
        elif schedule_type == "recurring":
            if not cron_expr:
                raise ValueError("cron_expr is required for recurring schedules")
            next_run = self._compute_next_run(cron_expr)
        else:
            raise ValueError(f"Unknown schedule_type: {schedule_type}")

        doc = {
            "name": name,
            "schedule_type": schedule_type,
            "action": action,
            "params": params,
            "cron_expr": cron_expr,
            "enabled": True,
            "next_run_at": next_run,
            "last_run_at": None,
            "run_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        result = await self.db.schedules.insert_one(doc)
        logger.info(f"Created schedule '{name}' (id={result.inserted_id}, type={schedule_type})")
        return str(result.inserted_id)

    async def get_schedule(self, schedule_id: str) -> Optional[dict]:
        """Get a single schedule by ID."""
        try:
            oid = ObjectId(schedule_id)
        except Exception:
            return None
        return await self.db.schedules.find_one({"_id": oid})

    async def delete_schedule(self, schedule_id: str) -> bool:
        """Delete a schedule by ID."""
        try:
            oid = ObjectId(schedule_id)
        except Exception:
            return False
        result = await self.db.schedules.delete_one({"_id": oid})
        if result.deleted_count > 0:
            logger.info(f"Deleted schedule {schedule_id}")
            return True
        return False

    async def list_schedules(self, enabled_only: bool = False) -> list[dict]:
        """List schedules, optionally filtering to enabled only."""
        query = {"enabled": True} if enabled_only else {}
        cursor = self.db.schedules.find(query).sort("next_run_at", 1)
        return await cursor.to_list(length=100)

    async def toggle_schedule(self, schedule_id: str, enabled: bool) -> bool:
        """Enable or disable a schedule."""
        try:
            oid = ObjectId(schedule_id)
        except Exception:
            return False
        updates: dict = {"enabled": enabled, "updated_at": datetime.now(timezone.utc)}
        if enabled:
            doc = await self.db.schedules.find_one({"_id": oid})
            if doc and doc.get("schedule_type") == "recurring" and doc.get("cron_expr"):
                updates["next_run_at"] = self._compute_next_run(doc["cron_expr"])
        result = await self.db.schedules.update_one({"_id": oid}, {"$set": updates})
        return result.matched_count > 0

    async def parse_reminder(self, text: str) -> Optional[dict]:
        """Parse natural language like 'remind me to X in Y minutes'.

        Returns a dict with keys (name, schedule_type, action, params, run_at/cron_expr)
        or None if the text doesn't match any known pattern.
        """
        lower = text.strip().lower()

        # "remind me to X in Y minutes/hours"
        match = re.match(
            r"remind\s+me\s+to\s+(.+?)\s+in\s+(\d+)\s*(m(?:in(?:ute)?s?)?|h(?:(?:ou)?rs?)?)\s*$",
            lower,
        )
        if match:
            message = match.group(1).strip()
            amount = int(match.group(2))
            unit = match.group(3)
            if unit.startswith("h"):
                delta = timedelta(hours=amount)
            else:
                delta = timedelta(minutes=amount)
            return {
                "name": f"Reminder: {message}",
                "schedule_type": "once",
                "action": "remind",
                "params": {"message": message},
                "run_at": datetime.now(timezone.utc) + delta,
            }

        # "remind me to X at HH:MM"
        match = re.match(
            r"remind\s+me\s+to\s+(.+?)\s+at\s+(\d{1,2}):(\d{2})\s*$",
            lower,
        )
        if match:
            message = match.group(1).strip()
            hour = int(match.group(2))
            minute = int(match.group(3))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            now = datetime.now(timezone.utc)
            run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if run_at <= now:
                run_at += timedelta(days=1)
            return {
                "name": f"Reminder: {message}",
                "schedule_type": "once",
                "action": "remind",
                "params": {"message": message},
                "run_at": run_at,
            }

        # "every X minutes/hours do Y" or "every morning check Y"
        match = re.match(
            r"every\s+(\d+)\s*(m(?:in(?:ute)?s?)?|h(?:(?:ou)?rs?)?)\s+(?:do\s+)?(.+)$",
            lower,
        )
        if match:
            amount = int(match.group(1))
            unit = match.group(2)
            message = match.group(3).strip()
            if unit.startswith("h"):
                cron_expr = f"every {amount}h"
            else:
                cron_expr = f"every {amount}m"
            return {
                "name": f"Recurring: {message}",
                "schedule_type": "recurring",
                "action": "remind",
                "params": {"message": message},
                "cron_expr": cron_expr,
            }

        return None
