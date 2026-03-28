"""
ARIA - Codex CLI Backend
"""

from __future__ import annotations

from aria.agents.backends.base import CommandSpec, StartParams
from aria.config import settings


class CodexBackend:
    name = "codex"

    def start_command(self, params: StartParams) -> CommandSpec:
        argv = [
            settings.codex_binary,
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            "-p",
            params.prompt,
        ]
        if params.model:
            argv.extend(["--model", params.model])
        return CommandSpec(argv=argv, cwd=params.workspace)

    def resume_command(self, session_id: str, params: StartParams) -> CommandSpec:
        argv = [settings.codex_binary, "resume", session_id, "-p", params.prompt]
        if params.model:
            argv.extend(["--model", params.model])
        return CommandSpec(argv=argv, cwd=params.workspace)

    def matches_process(self, cmdline: str) -> bool:
        return "codex" in cmdline
