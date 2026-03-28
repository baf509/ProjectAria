"""
ARIA - Soul Tool

Purpose: Allow ARIA to read and update its own identity document (SOUL.md).
"""

import logging

from ..base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType
from aria.core.soul import soul_manager

logger = logging.getLogger(__name__)


class SoulTool(BaseTool):
    """Read or update ARIA's identity document (SOUL.md)."""

    @property
    def name(self) -> str:
        return "update_soul"

    @property
    def description(self) -> str:
        return (
            "Read or update ARIA's identity document (SOUL.md). "
            "Use 'read' to view current identity, or 'write' to update it. "
            "Always tell the user when updating SOUL.md — it defines who you are."
        )

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="Whether to read or write the SOUL.md file",
                required=True,
                enum=["read", "write"],
            ),
            ToolParameter(
                name="content",
                type="string",
                description="New content for SOUL.md (required when action is 'write')",
                required=False,
            ),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        action = arguments.get("action", "read")

        if action == "read":
            content = soul_manager.read()
            if not content:
                return ToolResult(
                    tool_name=self.name,
                    status=ToolStatus.SUCCESS,
                    output="SOUL.md does not exist or is empty.",
                )
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.SUCCESS,
                output=content,
                metadata={"path": str(soul_manager.path)},
            )

        elif action == "write":
            content = arguments.get("content")
            if not content:
                return ToolResult(
                    tool_name=self.name,
                    status=ToolStatus.ERROR,
                    error="Content is required when action is 'write'.",
                )
            path = soul_manager.write(content)
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.SUCCESS,
                output=f"SOUL.md updated at {path}. Remember to tell the user about this change.",
                metadata={"path": path},
            )

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.ERROR,
            error=f"Unknown action: {action}. Use 'read' or 'write'.",
        )
