"""
ARIA - Shell Tool

Phase: 3
Purpose: Built-in tool for executing shell commands

Related Spec Sections:
- Section 8.3: Phase 3 - Tools & MCP

Safety:
- Timeout enforcement to prevent hanging processes
- Can be configured with allowed/denied commands
- Captures stdout and stderr
- Returns exit code for error handling
"""

import asyncio
import shlex
from typing import Optional
from ..base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType
import logging

logger = logging.getLogger(__name__)


class ShellTool(BaseTool):
    """
    Built-in tool for executing shell commands.

    Safety features:
    - Configurable timeout (default: 60 seconds)
    - Command filtering (optional allow/deny lists)
    - Captures both stdout and stderr
    - Reports exit codes
    """

    def __init__(
        self,
        timeout_seconds: int = 60,
        allowed_commands: Optional[list[str]] = None,
        denied_commands: Optional[list[str]] = None,
        working_directory: Optional[str] = None,
    ):
        """
        Initialize shell tool.

        Args:
            timeout_seconds: Maximum execution time for commands
            allowed_commands: List of allowed command prefixes (None = all allowed)
            denied_commands: List of denied command prefixes
            working_directory: Default working directory for commands
        """
        super().__init__()
        self.timeout_seconds = timeout_seconds
        self.allowed_commands = allowed_commands
        self.denied_commands = denied_commands or []
        self.working_directory = working_directory

        logger.info(
            f"Initialized ShellTool with timeout={timeout_seconds}s, "
            f"allowed_commands={allowed_commands}, denied_commands={denied_commands}"
        )

    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        return (
            "Execute shell commands and capture their output. "
            "Returns stdout, stderr, and exit code."
        )

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="command",
                type="string",
                description="The shell command to execute",
                required=True,
            ),
            ToolParameter(
                name="working_directory",
                type="string",
                description="Working directory for the command (optional)",
                required=False,
            ),
            ToolParameter(
                name="timeout",
                type="number",
                description="Command timeout in seconds (optional, overrides default)",
                required=False,
            ),
        ]

    def _validate_command(self, command: str) -> tuple[bool, Optional[str]]:
        """
        Validate that a command is allowed.

        Returns:
            (is_valid, error_message)
        """
        # Check denied commands first
        for denied in self.denied_commands:
            if command.startswith(denied):
                return False, f"Command denied: starts with '{denied}'"

        # Check allowed commands if specified
        if self.allowed_commands is not None:
            allowed = False
            for allowed_prefix in self.allowed_commands:
                if command.startswith(allowed_prefix):
                    allowed = True
                    break

            if not allowed:
                return False, f"Command not in allowed list"

        return True, None

    async def execute(self, arguments: dict) -> ToolResult:
        """Execute the shell command."""
        command = arguments.get("command", "")
        working_dir = arguments.get("working_directory", self.working_directory)
        timeout = arguments.get("timeout", self.timeout_seconds)

        # Validate command
        is_valid, error_msg = self._validate_command(command)
        if not is_valid:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=error_msg,
            )

        try:
            logger.info(f"Executing shell command: {command}")

            # Execute the command
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )

            # Wait for completion with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Kill the process if it times out
                process.kill()
                await process.wait()

                return ToolResult(
                    tool_name=self.name,
                    status=ToolStatus.ERROR,
                    error=f"Command timed out after {timeout} seconds",
                    metadata={
                        "command": command,
                        "timeout": timeout,
                    },
                )

            # Decode output
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            exit_code = process.returncode

            # Determine success/failure based on exit code
            status = ToolStatus.SUCCESS if exit_code == 0 else ToolStatus.ERROR

            output = {
                "stdout": stdout_text,
                "stderr": stderr_text,
                "exit_code": exit_code,
            }

            error = None
            if exit_code != 0:
                error = f"Command exited with code {exit_code}"
                if stderr_text:
                    error += f": {stderr_text[:200]}"  # First 200 chars of stderr

            logger.info(
                f"Shell command completed with exit_code={exit_code}, "
                f"stdout_len={len(stdout_text)}, stderr_len={len(stderr_text)}"
            )

            return ToolResult(
                tool_name=self.name,
                status=status,
                output=output,
                error=error,
                metadata={
                    "command": command,
                    "exit_code": exit_code,
                    "working_directory": working_dir,
                },
            )

        except Exception as e:
            logger.error(f"Shell command failed: {str(e)}", exc_info=True)
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Command execution failed: {str(e)}",
                metadata={"command": command},
            )
