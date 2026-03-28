"""
ARIA - Coding Backend Registry
"""

from __future__ import annotations

from aria.agents.backends.claude_code import ClaudeCodeBackend
from aria.agents.backends.codex import CodexBackend


class BackendRegistry:
    """Lazy registry for coding backends."""

    def __init__(self):
        self._backends = {
            "codex": CodexBackend(),
            "claude_code": ClaudeCodeBackend(),
        }

    def get(self, name: str):
        backend = self._backends.get(name)
        if backend is None:
            raise ValueError(f"Unknown coding backend: {name}")
        return backend

    def list(self) -> list[str]:
        return sorted(self._backends.keys())
