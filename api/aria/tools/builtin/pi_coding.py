"""
ARIA - Pi Coding Agent Tool

Purpose: Delegate coding tasks to the Pi Coding Agent, which runs on
Ridge's RTX 3090 (backend=ridge; see db.agents slug="pi-coding"). Creates
a persistent conversation that the user can jump into and continue
interacting with.

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
    Delegate a coding task to the Pi Coding Agent (Ridge-backed local LLM).

    Creates a conversation with the Pi agent, sends the task through
    the orchestrator using the local LLM backend, and returns the
    response along with a conversation ID the user can jump into.

    Unlike the Claude Agent tool (single-shot subprocess), this creates
    a persistent conversation that supports follow-up interaction. And
    unlike the pi-coding-ridge CODING SESSION (a distinct thing, started via
    start_coding_session(subagent_profile="pi-coding-ridge")), this tool has
    no filesystem/shell tools — it's chat-only, same as before the model
    backing it moved from laguna to Ridge.
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
            "Delegate a coding task to the Pi Coding Agent (local LLM, on Ridge's "
            "RTX 3090). Creates a persistent conversation the user can continue "
            "later. Use for brainstorming, architecture discussion, code review "
            "chat, or when the user wants a private local conversation — this is "
            "chat-only, it cannot write files or run commands. "
            "For a local-model agent that should actually make and verify "
            "changes, start a coding session with subagent_profile="
            "'pi-coding-ridge' instead; prefer claude_agent for the hardest tasks."
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

        # Safety gates (fail closed): block delegation while the killswitch or
        # the automated emergency stop is engaged.
        try:
            from aria.api.deps import get_killswitch, resolve_estop_manager
            get_killswitch().check_or_raise("pi coding delegation")
            estop = await resolve_estop_manager(self._db)
            estop_active = await estop.is_active()
            estop_reason = (await estop.get_state()).reason if estop_active else None
        except RuntimeError as exc:
            return ToolResult(tool_name=self.name, status=ToolStatus.ERROR, error=str(exc))
        except Exception as exc:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Safety check failed, refusing to delegate: {exc}",
            )
        if estop_active:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Emergency stop active: {estop_reason}. Delegation paused.",
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
