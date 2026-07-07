"""
ARIA - Node command queue (db.shell_commands)

The pull-based channel the central API uses to drive shells that live on a
remote node. The API enqueues a command and awaits its result; the node
long-polls for its pending commands, executes them against its local tmux, and
posts the result back. Commands carry a TTL so a dead node never accumulates a
backlog.

Pure functions over `db` so both ShellService (enqueue/await, caller side) and
NodeService (claim/complete, node side) can use them without a circular import.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from pymongo import ReturnDocument

from aria.config import settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue_command(
    db, node_id: str, kind: str, args: dict, *, ttl_seconds: Optional[int] = None
) -> str:
    """Insert a pending command for `node_id`; returns its id."""
    ttl = ttl_seconds if ttl_seconds is not None else settings.node_command_ttl_seconds
    now = _now()
    cmd_id = str(uuid4())
    await db.shell_commands.insert_one(
        {
            "_id": cmd_id,
            "node_id": node_id,
            "kind": kind,
            "args": args or {},
            "status": "pending",
            "result": None,
            "error": None,
            "created_at": now,
            "claimed_at": None,
            "done_at": None,
            "expires_at": now + timedelta(seconds=ttl),
        }
    )
    return cmd_id


async def await_result(
    db, cmd_id: str, *, timeout_seconds: Optional[int] = None
) -> Optional[dict]:
    """Poll until the command is done/error or the timeout elapses. Returns the
    command doc (with `result`/`error`), or None on timeout / missing."""
    timeout = timeout_seconds if timeout_seconds is not None else settings.node_command_timeout_seconds
    deadline = _now() + timedelta(seconds=timeout)
    while _now() < deadline:
        doc = await db.shell_commands.find_one({"_id": cmd_id})
        if not doc:
            return None
        if doc.get("status") in ("done", "error"):
            return doc
        await asyncio.sleep(0.15)
    return None


async def claim_commands(
    db, node_id: str, *, poll_seconds: Optional[int] = None
) -> list[dict]:
    """Long-poll: claim all pending commands for `node_id`, holding the request
    up to `poll_seconds` for the first one to arrive. Returns [] on timeout."""
    poll = poll_seconds if poll_seconds is not None else settings.node_command_poll_seconds
    deadline = _now() + timedelta(seconds=poll)
    while True:
        claimed: list[dict] = []
        while True:
            doc = await db.shell_commands.find_one_and_update(
                {"node_id": node_id, "status": "pending"},
                {"$set": {"status": "claimed", "claimed_at": _now()}},
                return_document=ReturnDocument.AFTER,
            )
            if not doc:
                break
            claimed.append(doc)
        if claimed:
            return claimed
        if _now() >= deadline:
            return []
        await asyncio.sleep(0.5)


async def complete_command(
    db, cmd_id: str, *, result: Optional[dict] = None, error: Optional[str] = None
) -> bool:
    """Mark a command done (or error) and store its result."""
    res = await db.shell_commands.update_one(
        {"_id": cmd_id},
        {
            "$set": {
                "status": "error" if error else "done",
                "result": result,
                "error": error,
                "done_at": _now(),
            }
        },
    )
    return res.matched_count > 0
