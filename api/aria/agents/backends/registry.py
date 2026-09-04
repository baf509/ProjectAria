"""
ARIA - Coding Backend Registry
"""

from __future__ import annotations

from aria.agents.backends.claude_code import ClaudeCodeBackend
from aria.agents.backends.codex import CodexBackend
from aria.agents.backends.pi_code import PiCodeBackend
from aria.agents.backends.pool import PoolBackend


class UnknownCodingBackendError(ValueError):
    def __init__(self, requested: str, valid: list[str], aliases: list[str]):
        self.requested = requested
        self.valid = valid
        self.aliases = aliases
        super().__init__(
            f"Unknown coding backend: {requested}. Valid: {', '.join(valid)} "
            f"(aliases: {', '.join(aliases)})"
        )


class CodingBackendUnavailableError(RuntimeError):
    def __init__(self, backend: str, reason: str, *, retryable: bool = False):
        self.backend = backend
        self.reason = reason
        self.retryable = retryable
        super().__init__(f"Coding backend {backend} is unavailable: {reason}")


class BackendRegistry:
    """Lazy registry for coding backends."""

    def __init__(self):
        self._backends = {
            "codex": CodexBackend(),
            "claude_code": ClaudeCodeBackend(),
            "pi-code": PiCodeBackend(),
            "pool": PoolBackend(),
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
        # Spellings the tool docstrings and Hermes hints may use.
        "pool-cli": "pool",
        "poolside": "pool",
    }

    def canonicalize(self, name: str | None) -> str | None:
        """Map an alias to its canonical backend key, leaving others untouched.

        Call this BEFORE any logic that compares a backend against a canonical
        name. `routing.is_routable_backend` checks membership in
        `{"claude_code"}`, so an un-normalized "claude-code" silently skipped
        complexity routing entirely — the same class of regression CLAUDE.md
        already documents for Hermes, reachable again through the alias.
        """
        if name is None:
            return None
        return self._ALIASES.get(name, name)

    def get(self, name: str):
        key = self._ALIASES.get(name, name)
        backend = self._backends.get(key)
        if backend is None:
            raise UnknownCodingBackendError(
                name,
                sorted(self._backends),
                sorted(self._ALIASES),
            )
        return backend

    def list(self) -> list[str]:
        return sorted(self._backends.keys())

    def is_registered(self, name: str | None) -> bool:
        """True if `name` (or its alias) is an actual coding-session backend.

        Used to tell a coding-session substrate (claude_code/codex/pi-code/pool)
        apart from an LLM-adapter name (llamacpp/agentic/ridge/anthropic/...) —
        the two vocabularies look interchangeable but are not; see the
        subagent_profile resolution in agents/session.py.
        """
        if name is None:
            return False
        return self._ALIASES.get(name, name) in self._backends
