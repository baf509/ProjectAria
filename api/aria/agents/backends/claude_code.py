"""
ARIA - Claude Code CLI Backend
"""

from __future__ import annotations

from aria.agents.backends.base import CommandSpec, StartParams
from aria.config import settings

# Marks a process ARIA launched. The shell substrate runs the agent under
# `bash -lc`, which sources the user's rc files — where the desk-path routing
# wrapper defines a `claude` shell function. Without this flag the wrapper
# would call back into ARIA and spawn another session, recursively.
MANAGED_ENV = {"ARIA_MANAGED": "1"}


class ClaudeCodeBackend:
    name = "claude_code"

    def start_command(self, params: StartParams) -> CommandSpec:
        argv = [settings.claude_code_binary, "--dangerously-skip-permissions"]
        if params.model:
            argv.extend(["--model", params.model])
        argv.extend(["-p", params.prompt])
        return CommandSpec(argv=argv, cwd=params.workspace, env=dict(MANAGED_ENV))

    def resume_command(self, session_id: str, params: StartParams) -> CommandSpec:
        argv = [settings.claude_code_binary, "--dangerously-skip-permissions",
                "--session-id", session_id, "--resume", "-p", params.prompt]
        if params.model:
            argv.extend(["--model", params.model])
        return CommandSpec(argv=argv, cwd=params.workspace, env=dict(MANAGED_ENV))

    def matches_process(self, cmdline: str) -> bool:
        return "claude" in cmdline
