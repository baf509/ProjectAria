"""
ARIA - Pi-Code Backend

Purpose: Marker backend for pi-code coding sessions. Unlike claude_code/codex
(whose start_command builds an argv the manager execs directly), pi-code has
no fixed CLI invocation -- its "command" (an `aria pi-code run` invocation
driving ARIA's own agentic loop against a pinned LLM) depends on a
conversation the manager creates first. CodingSessionManager branches on
`needs_custom_launch` and calls `_start_pi_code_session`/`_launch_pi_code_shell`
instead of the generic start_command()-based path. As of 2026-07-30 pi-code
DOES run on the shell substrate like every other backend (a real tmux pane) --
this flag is about launch-sequencing, not in-process-vs-subprocess.
"""

from __future__ import annotations

from aria.agents.backends.base import CommandSpec, StartParams


class PiCodeBackend:
    """ARIA's own agentic-loop backend -- custom launch sequencing (see module docstring)."""

    name = "pi-code"
    needs_custom_launch = True

    def start_command(self, params: StartParams) -> CommandSpec:  # pragma: no cover
        raise NotImplementedError("pi-code has a custom launch path; see _start_pi_code_session")

    def resume_command(self, session_id: str, params: StartParams) -> CommandSpec:  # pragma: no cover
        raise NotImplementedError("pi-code has a custom launch path; see _start_pi_code_session")

    def matches_process(self, cmdline: str) -> bool:
        return False
