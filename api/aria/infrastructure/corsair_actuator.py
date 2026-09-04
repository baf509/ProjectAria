"""Forced-command SSH actuator for Corsair model lifecycle operations.

This module is intentionally a tiny capability boundary.  The SSH key that
invokes it is configured with ``restrict`` and a forced command, so callers do
not get a shell, forwarding, a PTY, agent forwarding, or arbitrary argv.  The
only accepted requests are status/start/stop for a static, on-Corsair registry
slug.  Actual lifecycle work is delegated to :class:`ModelServerManager`, which
preserves the existing memory-pool, exclusivity, port, and launch guards.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from dataclasses import dataclass

from aria.infrastructure.model_servers import (
    ModelServerError,
    ModelServerManager,
    ModelServerNotFound,
    ModelServerSafetyError,
)


class ActuatorRequestError(ValueError):
    """The forced SSH command was not one of the allowed capabilities."""


@dataclass(frozen=True)
class ActuatorRequest:
    action: str
    slug: str
    force: bool = False


def parse_request(raw: str, manager: ModelServerManager) -> ActuatorRequest:
    """Parse the untrusted ``SSH_ORIGINAL_COMMAND`` without invoking a shell."""
    try:
        parts = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise ActuatorRequestError(f"invalid command encoding: {exc}") from exc

    if parts[:1] == ["aria-model-actuator"]:
        parts = parts[1:]
    if len(parts) not in (2, 3):
        raise ActuatorRequestError("expected: <status|start|stop> <registry-slug> [--force]")

    action, slug = parts[:2]
    if action not in {"status", "start", "stop"}:
        raise ActuatorRequestError(f"action not allowed: {action!r}")
    force = len(parts) == 3 and parts[2] == "--force"
    if len(parts) == 3 and not force:
        raise ActuatorRequestError("only the literal --force option is allowed")
    if force and action != "start":
        raise ActuatorRequestError("--force is valid only for start")

    # get_spec is an exact lookup in the static registry.  Dynamic database
    # rows are deliberately excluded from this remote capability boundary.
    spec = manager.get_spec(slug)
    if not spec.onbox:
        raise ActuatorRequestError(f"registry slug is not hosted on Corsair: {slug}")
    return ActuatorRequest(action=action, slug=slug, force=force)


async def execute(request: ActuatorRequest, manager: ModelServerManager) -> dict:
    if request.action == "status":
        return await manager.one(request.slug)
    if request.action == "start":
        return await manager.start(request.slug, force=request.force)
    return await manager.stop(request.slug)


def _emit(payload: dict) -> None:
    # One compact JSON line makes the response unambiguous for the Mac client
    # and prevents log chatter from being mistaken for the operation result.
    print(json.dumps(payload, separators=(",", ":"), default=str), flush=True)


def main() -> int:
    manager = ModelServerManager()
    try:
        request = parse_request(os.environ.get("SSH_ORIGINAL_COMMAND", ""), manager)
        result = asyncio.run(execute(request, manager))
    except (ActuatorRequestError, ModelServerNotFound) as exc:
        _emit({"ok": False, "kind": "request", "error": str(exc)})
        return 64
    except ModelServerSafetyError as exc:
        _emit({"ok": False, "kind": "safety", "error": str(exc)})
        return 65
    except ModelServerError as exc:
        _emit({"ok": False, "kind": "operation", "error": str(exc)})
        return 70
    except Exception as exc:  # fail closed; never expose a traceback over SSH
        _emit({"ok": False, "kind": "internal", "error": type(exc).__name__})
        return 70

    _emit({"ok": True, "result": result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
