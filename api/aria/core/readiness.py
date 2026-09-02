"""Process-local ARIA boot readiness state.

The API process has a long lifespan startup because it must connect to MongoDB,
run migrations, and restore its workers before it can safely accept mutations.
Keeping that state explicit gives launchers a stable readiness contract instead
of forcing them to guess from connection errors.
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any
from uuid import uuid4


_lock = Lock()
_state: dict[str, Any] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def reset() -> None:
    """Begin a new boot attempt."""
    with _lock:
        _state.clear()
        _state.update(
            {
                "boot_id": uuid4().hex,
                "ready": False,
                "phase": "process_starting",
                "blocked_on": "application startup",
                "started_at": _now(),
                "ready_at": None,
                "error": None,
            }
        )


def mark_phase(phase: str, blocked_on: str | None = None) -> None:
    with _lock:
        _state.update(
            {
                "ready": False,
                "phase": phase,
                "blocked_on": blocked_on,
                "ready_at": None,
                "error": None,
            }
        )


def mark_ready() -> None:
    with _lock:
        _state.update(
            {
                "ready": True,
                "phase": "ready",
                "blocked_on": None,
                "ready_at": _now(),
                "error": None,
            }
        )


def mark_failed(exc: BaseException) -> None:
    with _lock:
        _state.update(
            {
                "ready": False,
                "phase": "failed",
                "blocked_on": None,
                "ready_at": None,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
        )


def snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_state)


reset()
