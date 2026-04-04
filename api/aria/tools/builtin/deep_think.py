"""
ARIA - Deep Think Tool

Purpose: Delegate reasoning and analysis to Claude Opus via the Claude Code CLI.

This is the primary mechanism for ARIA's "hybrid brain" architecture:
the orchestrator model (cheap/fast via OpenRouter) handles conversation flow,
tool routing, and memory, while Claude Opus handles substantive reasoning,
analysis, and complex questions via subscription tokens.
"""

import logging

from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.tools.base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType

logger = logging.getLogger(__name__)


class DeepThinkTool(BaseTool):
    """
    Delegate reasoning to Claude Opus via the Claude Code CLI.

    This tool is the backbone of ARIA's hybrid architecture. The orchestrator
    model handles orchestration (memory retrieval, tool calls, streaming),
    while deep_think sends the actual question/analysis to Claude Opus
    using the user's Claude subscription (no API tokens consumed).

    Use for: answering questions, analysis, reasoning, explanations,
    writing, code review, planning — essentially any response that
    requires real intelligence rather than just routing.
    """

    def __init__(self, timeout_seconds: int = None):
        super().__init__()
        self.timeout = timeout_seconds or settings.deep_think_timeout_seconds

    @property
    def name(self) -> str:
        return "deep_think"

    @property
    def description(self) -> str:
        return (
            "Send a question or task to Claude Opus for deep reasoning. "
            "Use this for ALL substantive thinking: answering questions, analysis, "
            "explanations, writing, planning, code review, creative tasks, or any "
            "response requiring real intelligence. Include all relevant context "
            "(memories, conversation history, user's question) in the prompt. "
            "Returns Claude's response which you should relay to the user. "
            "Does not consume API tokens — uses the user's Claude subscription."
        )

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="prompt",
                type="string",
                description=(
                    "The complete prompt for Claude Opus. Include: "
                    "(1) the user's question or request, "
                    "(2) any relevant context from memories or conversation, "
                    "(3) any specific instructions about format or depth. "
                    "Be thorough — Claude has no other context beyond what you provide here."
                ),
                required=True,
            ),
            ToolParameter(
                name="context",
                type="string",
                description=(
                    "Additional context to prepend (memories, conversation history, "
                    "identity info). Optional but recommended for better responses."
                ),
                required=False,
            ),
        ]

    @property
    def dependencies(self) -> list[str]:
        return ["claude_cli"]

    async def execute(self, arguments: dict) -> ToolResult:
        prompt = arguments.get("prompt", "")
        if not prompt:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error="Prompt is required",
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

        # Prepend context if provided
        context = arguments.get("context", "")
        full_prompt = prompt
        if context:
            full_prompt = f"{context}\n\n---\n\n{prompt}"

        runner = ClaudeRunner(
            model=settings.deep_think_model or None,
            timeout_seconds=self.timeout,
        )

        logger.info(
            "deep_think delegating to Claude (timeout=%ds): %s",
            self.timeout, prompt[:200],
        )

        result = await runner.run(full_prompt)

        if result is None:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error="Claude returned no output (may have timed out or failed)",
            )

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output=result,
            metadata={
                "response_length": len(result),
                "model": settings.deep_think_model or "default",
            },
        )
