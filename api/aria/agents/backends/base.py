"""
ARIA - Coding Agent Backend Base

Purpose: Command generation protocol for coding backends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class StartParams:
    workspace: str
    prompt: str
    model: Optional[str] = None
    branch: Optional[str] = None
    # Provider name is used by external multi-provider CLIs such as Pi. It is
    # deliberately separate from `model`: ARIA's coding-session backend
    # (`pi-code`) and its LLM/provider (`agentic`, `ridge`, ...) are different
    # routing dimensions.
    provider: Optional[str] = None
    append_system_prompt: Optional[str] = None
    session_id: Optional[str] = None


@dataclass
class CommandSpec:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None


class AgentBackend(Protocol):
    name: str

    def start_command(self, params: StartParams) -> CommandSpec:
        ...

    def resume_command(self, session_id: str, params: StartParams) -> CommandSpec:
        ...

    def matches_process(self, cmdline: str) -> bool:
        ...

    def is_expected_failure_exit_code(self, exit_code: int) -> bool:
        """True if this exit code is a real (non-crash) result for this
        backend. Optional: callers must use getattr() with a False default,
        since most backends don't define one and Protocol provides no runtime
        default here."""
        ...
