"""
ARIA - Pi Coding Agent Tool

Purpose: Delegate coding tasks to the Pi Coding Agent which uses
the local LLM (llamacpp). Creates a persistent conversation that
the user can jump into and continue interacting with.

Inspired by pi-mono's coding assistant approach: structured thinking,
file-aware, progressive tool use, and iterative refinement.
"""

import logging
import uuid
from datetime import datetime, timezone

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.tools.base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType

logger = logging.getLogger(__name__)

PI_AGENT_SLUG = "pi-coding"


class PiCodingAgentTool(BaseTool):
    """
    Delegate a coding task to the Pi Coding Agent (local LLM).

    Creates a conversation with the Pi agent, sends the task through
    the orchestrator using the local LLM backend, and returns the
    response along with a conversation ID the user can jump into.

    Unlike the Claude Agent tool (single-shot subprocess), this creates
    a persistent conversation that supports follow-up interaction.
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        super().__init__()
        self._db = db

    @property
    def name(self) -> str:
        return "pi_coding_agent"

    @property
    def description(self) -> str:
        return (
            "Delegate a coding task to the Pi Coding Agent (local LLM). "
            "Creates a persistent conversation the user can jump into. "
            "Use for coding tasks, debugging, architecture design, "
            "refactoring, or any development work. Runs on the local LLM "
            "so it's free and private."
        )

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="task",
                type="string",
                description=(
                    "Detailed description of the coding task. Include relevant "
                    "context: file paths, error messages, desired behavior, "
                    "language/framework, and any constraints."
                ),
                required=True,
            ),
            ToolParameter(
                name="title",
                type="string",
                description="Title for the coding conversation (optional).",
                required=False,
            ),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        task = arguments.get("task", "")
        if not task:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error="Task description is required",
            )

        title = arguments.get("title") or f"Pi Coding: {task[:60]}..."

        # Find the Pi Coding Agent
        agent = await self._db.agents.find_one({"slug": PI_AGENT_SLUG})
        if not agent:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=(
                    f"Pi Coding Agent not found (slug='{PI_AGENT_SLUG}'). "
                    "Create the agent first via the admin API."
                ),
            )

        # Create a conversation with the Pi agent
        now = datetime.now(timezone.utc)
        conversation = {
            "agent_id": agent["_id"],
            "active_agent_id": None,
            "title": title,
            "summary": None,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "llm_config": {
                "backend": agent["llm"]["backend"],
                "model": agent["llm"]["model"],
                "temperature": agent["llm"]["temperature"],
            },
            "messages": [],
            "tags": ["pi-coding", "delegated"],
            "pinned": False,
            "stats": {"message_count": 0, "total_tokens": 0, "tool_calls": 0},
        }

        result = await self._db.conversations.insert_one(conversation)
        conversation_id = str(result.inserted_id)

        # Process the task through the orchestrator
        try:
            from aria.api.deps import get_tool_router, get_task_runner, get_coding_session_manager
            from aria.core.orchestrator import Orchestrator

            tool_router = get_tool_router()
            task_runner = await get_task_runner(self._db)
            coding_manager = await get_coding_session_manager(self._db)

            orchestrator = Orchestrator(
                db=self._db,
                tool_router=tool_router,
                task_runner=task_runner,
                coding_manager=coding_manager,
            )

            content_parts = []
            async for chunk in orchestrator.process_message(
                conversation_id, task, stream=False,
            ):
                if chunk.type == "text":
                    content_parts.append(chunk.content)

            response = "".join(content_parts)

            logger.info(
                "Pi Coding Agent completed task (conv=%s): %s",
                conversation_id, task[:100],
            )

            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.SUCCESS,
                output={
                    "response": response,
                    "conversation_id": conversation_id,
                    "agent": agent["name"],
                    "model": agent["llm"]["model"],
                    "message": (
                        f"The Pi Coding Agent has responded. "
                        f"The user can continue this conversation at: "
                        f"/conversations/{conversation_id}"
                    ),
                },
            )

        except Exception as e:
            logger.error("Pi Coding Agent failed: %s", e, exc_info=True)
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Pi Coding Agent failed: {e}",
                metadata={"conversation_id": conversation_id},
            )
