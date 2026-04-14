"""
ARIA - Autopilot Executor

Purpose: Execute autopilot plan steps with killswitch checks
and optional approval gating (safe mode).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.llm.base import Message
from aria.llm.manager import llm_manager

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorDatabase
    from aria.core.killswitch import Killswitch
    from aria.tools.router import ToolRouter

logger = logging.getLogger(__name__)


class AutopilotExecutor:
    """Execute plan steps with killswitch checks and approval gates."""

    def __init__(
        self,
        db: "AsyncIOMotorDatabase",
        killswitch: "Killswitch",
        tool_router: Optional["ToolRouter"] = None,
    ):
        self.db = db
        self.killswitch = killswitch
        self.tool_router = tool_router
        # Maps session_id -> step_index -> asyncio.Event for approval gating
        self._approval_gates: dict[str, dict[int, asyncio.Event]] = {}

    async def execute_plan(
        self,
        session_id: str,
        steps: list[dict],
        mode: str = "safe",
        backend: str = "llamacpp",
        model: str = "default",
    ) -> list[dict]:
        """Execute all steps in a plan."""
        results = []
        logger.info("Executing autopilot plan %s (%d steps, mode=%s)", session_id, len(steps), mode)

        for step in steps:
            # Check killswitch before each step
            self.killswitch.check_or_raise("autopilot step execution")

            step_index = step["index"]
            step_name = step.get("name", f"step-{step_index}")
            logger.info("Autopilot %s: starting step %d/%d (%s)", session_id, step_index + 1, len(steps), step_name)

            # Update step status
            await self._update_step(session_id, step_index, status="running")

            # Safe mode: wait for approval
            if mode == "safe":
                await self._update_step(session_id, step_index, status="awaiting_approval")

                # Create and wait on approval gate
                if session_id not in self._approval_gates:
                    self._approval_gates[session_id] = {}
                gate = asyncio.Event()
                self._approval_gates[session_id][step_index] = gate

                try:
                    await asyncio.wait_for(
                        gate.wait(),
                        timeout=settings.autopilot_step_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    await self._update_step(
                        session_id, step_index,
                        status="timed_out",
                        result="Approval timed out",
                    )
                    results.append({"index": step_index, "status": "timed_out"})
                    break
                finally:
                    self._approval_gates.get(session_id, {}).pop(step_index, None)

                await self._update_step(session_id, step_index, status="running")

            # Execute the step
            try:
                result = await self._execute_step(step, backend, model)
                await self._update_step(
                    session_id, step_index,
                    status="completed",
                    result=result,
                )
                step["status"] = "completed"
                step["result"] = result
                results.append({"index": step_index, "status": "completed", "result": result})
                logger.info("Autopilot %s: step %d completed", session_id, step_index)

            except Exception as exc:
                error = str(exc)
                logger.error("Autopilot %s: step %d failed: %s", session_id, step_index, error)
                await self._update_step(
                    session_id, step_index,
                    status="failed",
                    result=error,
                )
                step["status"] = "failed"
                step["result"] = error
                results.append({"index": step_index, "status": "failed", "error": error})
                break

        logger.info(
            "Autopilot %s: plan execution finished (%d/%d steps completed)",
            session_id, sum(1 for r in results if r["status"] == "completed"), len(steps),
        )
        return results

    async def _execute_step(self, step: dict, backend: str, model: str) -> str:
        """Execute a single plan step."""
        action = step.get("action", "llm_query")

        if action == "tool_call" and self.tool_router and step.get("tool_name"):
            result = await self.tool_router.execute_tool(
                tool_name=step["tool_name"],
                arguments=step.get("tool_arguments", {}),
            )
            return str(result.output) if result.output else (result.error or "No output")

        # Default: LLM query — use ClaudeRunner when available
        step_prompt = step.get("description", step.get("name", ""))

        if settings.use_claude_runner and ClaudeRunner.is_available():
            runner = ClaudeRunner(timeout_seconds=settings.autopilot_step_timeout_seconds)
            result = await runner.run(step_prompt)
            if result:
                return result.strip()

        # Fall back to API tokens
        adapter = llm_manager.get_adapter(backend, model)
        messages = [Message(role="user", content=step_prompt)]

        parts = []
        async for chunk in adapter.stream(
            messages, temperature=0.5, max_tokens=2048, stream=False
        ):
            if chunk.type == "text":
                parts.append(chunk.content)

        return "".join(parts).strip()

    def approve_step(self, session_id: str, step_index: int) -> bool:
        """Approve a step that is awaiting approval."""
        gate = self._approval_gates.get(session_id, {}).get(step_index)
        if gate is not None:
            gate.set()
            return True
        return False

    def cancel_session(self, session_id: str) -> None:
        """Cancel all pending approval gates for a session."""
        gates = self._approval_gates.pop(session_id, {})
        for gate in gates.values():
            gate.set()  # Unblock waiting tasks

    async def _update_step(
        self,
        session_id: str,
        step_index: int,
        status: str,
        result: Optional[str] = None,
    ) -> None:
        """Update a step's status in the database."""
        updates = {
            f"steps.{step_index}.status": status,
            "updated_at": datetime.now(timezone.utc),
        }
        if result is not None:
            updates[f"steps.{step_index}.result"] = result

        await self.db.autopilot_sessions.update_one(
            {"_id": session_id},
            {"$set": updates},
        )
