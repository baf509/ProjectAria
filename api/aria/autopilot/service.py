"""
ARIA - Autopilot Service

Purpose: Coordinate autopilot planner and executor, manage sessions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorDatabase
    from aria.core.killswitch import Killswitch
    from aria.tasks.runner import TaskRunner
    from aria.tools.router import ToolRouter

from aria.autopilot.planner import AutopilotPlanner
from aria.autopilot.executor import AutopilotExecutor
from aria.config import settings

logger = logging.getLogger(__name__)


class AutopilotService:
    """Coordinate autopilot sessions: planning, execution, and approval."""

    def __init__(
        self,
        db: "AsyncIOMotorDatabase",
        killswitch: "Killswitch",
        task_runner: "TaskRunner",
        tool_router: Optional["ToolRouter"] = None,
    ):
        self.db = db
        self.killswitch = killswitch
        self.task_runner = task_runner
        self.planner = AutopilotPlanner()
        self.executor = AutopilotExecutor(db, killswitch, tool_router)

    async def start(
        self,
        goal: str,
        mode: str = "safe",
        backend: str = "llamacpp",
        model: str = "default",
        context: str = "",
    ) -> dict:
        """Start an autopilot session: plan then execute."""
        self.killswitch.check_or_raise("autopilot start")

        if mode not in ("safe", "unrestricted"):
            raise ValueError("Mode must be 'safe' or 'unrestricted'")

        # Create plan
        steps = await self.planner.create_plan(
            goal=goal, backend=backend, model=model, context=context
        )

        if len(steps) > settings.autopilot_max_steps:
            steps = steps[: settings.autopilot_max_steps]

        # Create session document
        session_id = str(uuid4())
        now = datetime.now(timezone.utc)
        session = {
            "_id": session_id,
            "goal": goal,
            "mode": mode,
            "backend": backend,
            "model": model,
            "steps": steps,
            "status": "planned",
            "created_at": now,
            "updated_at": now,
        }
        await self.db.autopilot_sessions.insert_one(session)

        # Submit execution as a background task
        task_id = await self.task_runner.submit_task(
            name=f"autopilot:{goal[:60]}",
            coroutine_factory=lambda: self.executor.execute_plan(
                session_id=session_id,
                steps=steps,
                mode=mode,
                backend=backend,
                model=model,
            ),
            metadata={"task_kind": "autopilot", "session_id": session_id},
            timeout_seconds=settings.autopilot_step_timeout_seconds * len(steps),
        )

        await self.db.autopilot_sessions.update_one(
            {"_id": session_id},
            {"$set": {"status": "running", "task_id": task_id, "updated_at": now}},
        )

        return {
            "session_id": session_id,
            "task_id": task_id,
            "goal": goal,
            "mode": mode,
            "step_count": len(steps),
            "steps": [
                {"index": s["index"], "name": s["name"], "action": s["action"]}
                for s in steps
            ],
        }

    def approve_step(self, session_id: str, step_index: int) -> bool:
        """Approve a step in safe mode."""
        return self.executor.approve_step(session_id, step_index)

    async def stop(self, session_id: str) -> dict:
        """Stop an autopilot session."""
        session = await self.db.autopilot_sessions.find_one({"_id": session_id})
        if not session:
            raise ValueError("Session not found")

        self.executor.cancel_session(session_id)

        # Cancel the background task if running
        task_id = session.get("task_id")
        if task_id:
            await self.task_runner.cancel_task(task_id)

        await self.db.autopilot_sessions.update_one(
            {"_id": session_id},
            {"$set": {"status": "stopped", "updated_at": datetime.now(timezone.utc)}},
        )
        return {"session_id": session_id, "status": "stopped"}

    async def get_session(self, session_id: str) -> Optional[dict]:
        """Get session details."""
        session = await self.db.autopilot_sessions.find_one({"_id": session_id})
        if not session:
            return None
        return {
            "session_id": session["_id"],
            "goal": session["goal"],
            "mode": session["mode"],
            "status": session["status"],
            "steps": session.get("steps", []),
            "task_id": session.get("task_id"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
        }
