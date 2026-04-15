"""
ARIA - Shells Tools

Purpose: Allow ARIA to observe and interact with watched tmux shells.
"""

import logging

from ..base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType
from aria.shells.service import (
    ShellNotFoundError,
    ShellService,
    ShellStoppedError,
)

logger = logging.getLogger(__name__)


class SendShellInputTool(BaseTool):
    """Send keystrokes to a watched tmux shell (e.g. a Claude Code session)."""

    def __init__(self, shell_service: ShellService):
        self.shell_service = shell_service

    @property
    def name(self) -> str:
        return "send_shell_input"

    @property
    def description(self) -> str:
        return (
            "Send keystrokes to a watched tmux shell — useful for answering "
            "prompts in the user's coding sessions (e.g., Claude Code asking "
            "'yes/no?'). Use tmux key names like 'Enter', 'C-c', 'Up' for "
            "special keys, or set literal=true to send exact text. "
            "Always confirm with the user before sending destructive input."
        )

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="shell_name",
                type="string",
                description="Name of the watched shell (tmux session name)",
                required=True,
            ),
            ToolParameter(
                name="text",
                type="string",
                description="Text or tmux key name to send (e.g. 'yes', 'C-c', 'Enter')",
                required=True,
            ),
            ToolParameter(
                name="append_enter",
                type="boolean",
                description="Whether to append Enter after the text (default true)",
                required=False,
            ),
            ToolParameter(
                name="literal",
                type="boolean",
                description="Send text literally without key translation (default false)",
                required=False,
            ),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        name = arguments.get("shell_name", "").strip()
        text = arguments.get("text", "")
        append_enter = bool(arguments.get("append_enter", True))
        literal = bool(arguments.get("literal", False))

        if not name or not text:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error="shell_name and text are required.",
            )

        try:
            line_number = await self.shell_service.send_input(
                name, text, append_enter=append_enter, literal=literal
            )
        except ShellNotFoundError:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Shell '{name}' is not registered.",
            )
        except ShellStoppedError:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Shell '{name}' has stopped.",
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Failed to send input: {exc}",
            )

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output=f"Sent to {name} (line {line_number}).",
            metadata={"shell_name": name, "line_number": line_number},
        )
