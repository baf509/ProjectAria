"""
ARIA - Coding Backend Registry
"""

from __future__ import annotations

from aria.agents.backends.claude_code import ClaudeCodeBackend
from aria.agents.backends.codex import CodexBackend
from aria.agents.backends.pi_code import PiCodeBackend


class BackendRegistry:
    """Lazy registry for coding backends."""

    def __init__(self):
        self._backends = {
            "codex": CodexBackend(),
            "claude_code": ClaudeCodeBackend(),
            "pi-code": PiCodeBackend(),
        }

    # Accepted spellings that are not the canonical key. `pi` is here because
    # mcp/server.py's create_coding_session docstring advertised it as a valid
    # backend, so callers (Hermes included) reasonably sent it and got a 500
    # `ValueError: Unknown coding backend: pi` — the request was well-formed
    # against the documented interface. The docstring is corrected, but the
    # alias stays: a public tool description promised this spelling.
    _ALIASES = {
        "pi": "pi-code",
        "picode": "pi-code",
        "pi_code": "pi-code",
        "claude-code": "claude_code",
    }

    def get(self, name: str):
        key = self._ALIASES.get(name, name)
        backend = self._backends.get(key)
        if backend is None:
            raise ValueError(
                f"Unknown coding backend: {name}. "
                f"Valid: {', '.join(sorted(self._backends))} "
                f"(aliases: {', '.join(sorted(self._ALIASES))})"
            )
        return backend

    def list(self) -> list[str]:
        return sorted(self._backends.keys())
