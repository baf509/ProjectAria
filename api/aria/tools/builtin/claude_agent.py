"""
ARIA - Claude Agent Tool

Purpose: Allows ARIA to delegate complex tasks to a Claude Code CLI
subprocess. Uses the user's Claude Code subscription tokens.

This gives ARIA the ability to spawn a "thinking agent" for tasks like:
- Analyzing external repos or codebases
- Deep research and investigation
- Complex reasoning tasks
- Code review and architecture analysis
"""

import logging

from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.tools.base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType

logger = logging.getLogger(__name__)


class ClaudeAgentTool(BaseTool):
    """
    Delegate a task to a Claude Code CLI agent.

    Spawns `claude -p "prompt"` as a subprocess, using the user's
    Claude Code subscription. Designed for heavy-lifting tasks that
    benefit from a fresh context and dedicated reasoning.
    """

    def __init__(self, timeout_seconds: int = None):
        super().__init__()
        self.timeout = timeout_seconds or settings.claude_runner_timeout_seconds

    @property
    def name(self) -> str:
        return "claude_agent"

    @property
    def description(self) -> str:
        return (
            "Delegate a task to a Claude Code agent (subprocess). "
            "Use this for complex analysis, investigating repos or codebases, "
            "deep research, code review, or any task that benefits from "
            "dedicated reasoning. The agent runs independently and returns "
            "its findings. Does not consume API tokens — uses the user's "
            "Claude Code subscription."
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
                    "Detailed description of the task for the agent. "
                    "Be specific about what to investigate, analyze, or produce. "
                    "Include any relevant context, URLs, file paths, or constraints."
                ),
                required=True,
            ),
            ToolParameter(
                name="working_directory",
                type="string",
                description=(
                    "Working directory for the agent. Defaults to the ARIA workspace. "
                    "Set this to a repo path if the agent needs to read files there."
                ),
                required=False,
            ),
            ToolParameter(
                name="timeout_seconds",
                type="number",
                description=(
                    "Maximum time in seconds for the agent to run. "
                    "Default depends on server config (typically 120s). "
                    "Increase for complex tasks."
                ),
                required=False,
            ),
        ]

    @property
    def dependencies(self) -> list[str]:
        return ["claude_cli"]

    async def execute(self, arguments: dict) -> ToolResult:
        task = arguments.get("task", "")
        if not task:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error="Task description is required",
            )

        if not ClaudeRunner.is_available():
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=(
                    f"Claude Code CLI not found at '{settings.claude_code_binary}'. "
                    "Install Claude Code or set CLAUDE_CODE_BINARY in .env"
                ),
            )

        cwd = arguments.get("working_directory") or settings.coding_default_workspace
        timeout = int(arguments.get("timeout_seconds", 0)) or self.timeout

        runner = ClaudeRunner(timeout_seconds=timeout, cwd=cwd)

        logger.info(
            "Claude agent delegated task (timeout=%ds, cwd=%s): %s",
            timeout, cwd, task[:200],
        )

        result = await runner.run(task)

        if result is None:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error="Claude agent returned no output (may have timed out or failed)",
            )

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output={"response": result},
            metadata={
                "working_directory": cwd,
                "timeout_seconds": timeout,
                "response_length": len(result),
            },
        )
