"""
ARIA - Claude Code CLI Backend
"""

from __future__ import annotations

from aria.agents.backends.base import CommandSpec, StartParams
from aria.config import settings


class ClaudeCodeBackend:
    name = "claude_code"

    def start_command(self, params: StartParams) -> CommandSpec:
        argv = [settings.claude_code_binary, "--dangerously-skip-permissions"]
        if params.model:
            argv.extend(["--model", params.model])
        argv.extend(["-p", params.prompt])
        return CommandSpec(argv=argv, cwd=params.workspace)

    def resume_command(self, session_id: str, params: StartParams) -> CommandSpec:
        argv = [settings.claude_code_binary, "--dangerously-skip-permissions",
                "--session-id", session_id, "--resume", "-p", params.prompt]
        if params.model:
            argv.extend(["--model", params.model])
        return CommandSpec(argv=argv, cwd=params.workspace)

    def matches_process(self, cmdline: str) -> bool:
        return "claude" in cmdline
