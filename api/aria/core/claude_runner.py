"""
ARIA - Claude Code CLI Runner

Purpose: Shared utility for running heavy LLM tasks via Claude Code CLI
subprocess instead of consuming API tokens. Uses the user's Claude Code
subscription for background/non-interactive work.

Usage:
    runner = ClaudeRunner()
    output = await runner.run("Summarize this conversation: ...")

All background tasks (memory extraction, summarization, heartbeat,
OODA evaluation, research, autopilot planning) can use this instead
of the LLM adapter when configured.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aria.config import settings

logger = logging.getLogger(__name__)


class ClaudeRunner:
    """
    Run prompts through Claude Code CLI subprocess.

    This uses the user's Claude Code subscription tokens instead of
    API tokens (OpenRouter, Anthropic, etc). Designed for background
    tasks that don't need streaming to the user.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        cwd: Optional[str] = None,
    ):
        self.model = model or settings.dream_claude_model or ""
        self.timeout = timeout_seconds or settings.dream_timeout_seconds
        self.cwd = cwd or settings.coding_default_workspace

    async def run(self, prompt: str) -> Optional[str]:
        """
        Run a prompt through Claude Code CLI and return the output.

        Args:
            prompt: The full prompt to send to Claude

        Returns:
            Claude's text output, or None on failure
        """
        argv = [settings.claude_code_binary]
        if self.model:
            argv.extend(["--model", self.model])
        argv.extend(["-p", prompt])

        logger.debug("ClaudeRunner spawning: %s (timeout=%ds)", argv[0], self.timeout)

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                self.last_error = f"Timed out after {self.timeout}s"
                logger.error("ClaudeRunner timed out after %ds", self.timeout)
                return None

            if process.returncode != 0:
                stderr_text = stderr.decode("utf-8", errors="replace")[:500]
                self.last_error = f"Exit {process.returncode}: {stderr_text}"
                logger.error(
                    "ClaudeRunner failed (exit %d): %s",
                    process.returncode, stderr_text,
                )
                return None

            output = stdout.decode("utf-8", errors="replace").strip()
            logger.debug("ClaudeRunner output: %d chars", len(output))
            return output

        except FileNotFoundError:
            self.last_error = f"CLI not found at '{settings.claude_code_binary}'"
            logger.error(
                "Claude Code CLI not found at '%s'. "
                "Install Claude Code or set CLAUDE_CODE_BINARY in .env",
                settings.claude_code_binary,
            )
            return None
        except Exception as e:
            self.last_error = str(e)
            logger.error("ClaudeRunner error: %s", e)
            return None

    @staticmethod
    def is_available() -> bool:
        """Check if Claude Code CLI is likely available."""
        import shutil
        return shutil.which(settings.claude_code_binary) is not None
