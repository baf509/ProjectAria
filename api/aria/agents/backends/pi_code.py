"""
ARIA - Pi-Code In-Process Backend

Purpose: Marker backend for pi-code coding sessions. Unlike claude_code/codex
(which exec an external CLI), pi-code runs ARIA's own agentic loop in-process
with a pinned LLM (Fireworks GLM, qwen-agentic, or local qwen-chat). The
CodingSessionManager branches on `is_in_process` and never calls start_command.
"""

from __future__ import annotations

from aria.agents.backends.base import CommandSpec, StartParams


class PiCodeBackend:
    """In-process agentic-loop backend (no external CLI process)."""

    name = "pi-code"
    is_in_process = True

    def start_command(self, params: StartParams) -> CommandSpec:  # pragma: no cover
        raise NotImplementedError("pi-code runs in-process; it has no start command")

    def resume_command(self, session_id: str, params: StartParams) -> CommandSpec:  # pragma: no cover
        raise NotImplementedError("pi-code runs in-process; it has no resume command")

    def matches_process(self, cmdline: str) -> bool:
        return False
