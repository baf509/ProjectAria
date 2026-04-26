"""
ARIA - Watched Shells Service

Purpose: Business logic for shells — registration, event insertion, reads,
streaming, status reconciliation, and input dispatch.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Iterable, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.shells.ansi import strip_ansi
from aria.shells.models import Shell, ShellEvent, ShellSnapshot
from aria.shells.tmux import TmuxClient, TmuxError, TmuxSessionNotFoundError

logger = logging.getLogger(__name__)


class ShellNotFoundError(Exception):
    """Raised when a shell is not registered in the database."""


class ShellStoppedError(Exception):
    """Raised when operating on a shell whose tmux session has ended."""


class ShellAlreadyExistsError(Exception):
    """Raised when creating a shell whose tmux session already exists."""


def _strip_prefix(name: str, prefix: str) -> str:
    return name[len(prefix):] if name.startswith(prefix) else name


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ShellService:
    """Service for managing watched tmux shells."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        tmux: Optional[TmuxClient] = None,
    ):
        self.db = db
        self.tmux = tmux or TmuxClient()
        self.shells = db.shells
        self.events = db.shell_events
        self.snapshots = db.shell_snapshots

    # ------------------------------------------------------------------ reads

    async def list_shells(
        self,
        status: Optional[Iterable[str]] = None,
    ) -> list[Shell]:
        query: dict = {}
        if status:
            query["status"] = {"$in": list(status)}
        cursor = self.shells.find(query).sort("last_activity_at", -1)
        out: list[Shell] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(Shell(**doc))
        return out

    async def get_shell(self, name: str) -> Optional[Shell]:
        doc = await self.shells.find_one({"name": name})
        if not doc:
            return None
        doc.pop("_id", None)
        return Shell(**doc)

    async def list_events(
        self,
        name: str,
        *,
        since: Optional[datetime] = None,
        since_line: Optional[int] = None,
        before: Optional[datetime] = None,
        limit: int = 500,
        kinds: Optional[Iterable[str]] = None,
        sort: int = 1,
    ) -> list[ShellEvent]:
        query: dict = {"shell_name": name}
        if since is not None:
            query["ts"] = {"$gt": since}
        if before is not None:
            query.setdefault("ts", {})
            query["ts"]["$lt"] = before
        if since_line is not None:
            query["line_number"] = {"$gt": since_line}
        if kinds:
            query["kind"] = {"$in": list(kinds)}
        cursor = self.events.find(query).sort("line_number", sort).limit(int(limit))
        out: list[ShellEvent] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(ShellEvent(**doc))
        return out

    async def tail(self, name: str, *, lines: int = 100) -> list[ShellEvent]:
        """Return the last N events for a shell in chronological order."""
        cursor = (
            self.events.find({"shell_name": name})
            .sort("line_number", -1)
            .limit(int(lines))
        )
        out: list[ShellEvent] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(ShellEvent(**doc))
        out.reverse()
        return out

    async def get_last_snapshot(self, name: str) -> Optional[ShellSnapshot]:
        doc = await self.snapshots.find_one(
            {"shell_name": name}, sort=[("ts", -1)]
        )
        if not doc:
            return None
        doc.pop("_id", None)
        return ShellSnapshot(**doc)

    # --------------------------------------------------------------- lifecycle

    async def register_shell(
        self,
        name: str,
        *,
        project_dir: str = "",
        pane_id: str = "",
    ) -> Shell:
        """Create or refresh a shells doc for a tmux session.

        Idempotent — called on session-created and client-attached hooks.
        """
        now = _utcnow()
        prefix = settings.shells_tmux_session_prefix
        short = _strip_prefix(name, prefix)
        host = socket.gethostname()

        update = {
            "$setOnInsert": {
                "name": name,
                "short_name": short,
                "project_dir": project_dir,
                "host": host,
                "created_at": now,
                "line_count": 0,
                "tags": [],
            },
            "$set": {
                "status": "active",
                "last_activity_at": now,
            },
        }
        if pane_id:
            update.setdefault("$set", {})["metadata.pane_id"] = pane_id

        await self.shells.update_one({"name": name}, update, upsert=True)
        shell = await self.get_shell(name)
        assert shell is not None
        return shell

    async def mark_stopped(self, name: str) -> None:
        await self.shells.update_one(
            {"name": name},
            {"$set": {"status": "stopped", "last_activity_at": _utcnow()}},
        )
        await self.insert_events_batch(
            name,
            [
                {
                    "kind": "system",
                    "text_raw": "shell stopped",
                    "text_clean": "shell stopped",
                    "source": "hook",
                }
            ],
            update_shell_timestamps=False,
        )

    async def set_status(self, name: str, status: str) -> None:
        await self.shells.update_one(
            {"name": name}, {"$set": {"status": status}}
        )

    async def set_tags(self, name: str, tags: list[str]) -> None:
        await self.shells.update_one(
            {"name": name}, {"$set": {"tags": list(tags)}}
        )

    async def create_shell(
        self,
        name: str,
        *,
        workdir: str = "",
        launch_claude: bool = True,
        cols: Optional[int] = None,
        rows: Optional[int] = None,
    ) -> Shell:
        """Create a detached tmux session and register it as a watched shell.

        If the tmux session already exists, reclaim it unless Aria already
        tracks it as active/idle (true duplicate). Reclaim = register a
        missing Aria row, or reactivate a stopped one.

        `cols`/`rows` override the default tmux geometry from settings.
        Mobile clients should pass their actual viewport size; otherwise
        sessions are created at `shells_default_cols × shells_default_rows`
        (much wider than tmux's 80x24 default so TUIs don't wrap).
        """
        prefix = settings.shells_tmux_session_prefix
        full_name = name if name.startswith(prefix) else f"{prefix}{name}"

        if await self.tmux.has_session(full_name):
            existing = await self.get_shell(full_name)
            if existing and existing.status in ("active", "idle"):
                raise ShellAlreadyExistsError(full_name)
            logger.info("reclaiming orphan tmux session: %s", full_name)
            return await self.register_shell(full_name, project_dir=workdir or "")

        command = "claude --dangerously-skip-permissions" if launch_claude else None
        effective_cols = cols or settings.shells_default_cols
        effective_rows = rows or settings.shells_default_rows
        await self.tmux.new_session(
            full_name,
            workdir=workdir or None,
            command=command,
            cols=effective_cols,
            rows=effective_rows,
        )
        return await self.register_shell(full_name, project_dir=workdir or "")

    async def resize_shell(self, name: str, cols: int, rows: int) -> None:
        """Resize a shell's tmux window. Fires SIGWINCH so the running TUI repaints."""
        await self.tmux.resize_window(name, cols, rows)

    async def kill_shell(self, name: str) -> None:
        """Kill a tmux session and mark its shell row stopped.

        Idempotent — a missing tmux session is not an error, the shell
        row is still marked stopped.
        """
        await self.tmux.kill_session(name)
        await self.mark_stopped(name)

    async def purge_shell(self, name: str) -> dict:
        """Kill the tmux session and delete the shell row, events, and snapshots.

        Unlike `kill_shell`, this removes all history. Returns counts for
        each deletion.
        """
        try:
            await self.tmux.kill_session(name)
        except TmuxError as exc:  # pragma: no cover - defensive
            logger.debug("purge: kill-session failed for %s: %s", name, exc)
        s = await self.shells.delete_one({"name": name})
        e = await self.events.delete_many({"shell_name": name})
        n = await self.snapshots.delete_many({"shell_name": name})
        return {
            "shells": s.deleted_count,
            "events": e.deleted_count,
            "snapshots": n.deleted_count,
        }

    # -------------------------------------------------------------- write path

    async def insert_events_batch(
        self,
        name: str,
        events: list[dict],
        *,
        update_shell_timestamps: bool = True,
    ) -> int:
        """Append a batch of events to shell_events.

        `line_number` is assigned server-side using `$inc` on the parent
        shells doc. If the shell does not exist yet it is registered
        implicitly with an empty project_dir.
        """
        if not events:
            return 0

        now = _utcnow()
        count = len(events)

        # Atomically bump the counter and stamp last_activity_at.
        doc = await self.shells.find_one_and_update(
            {"name": name},
            {
                "$inc": {"line_count": count},
                "$set": {"last_activity_at": now} if update_shell_timestamps else {},
                "$setOnInsert": {
                    "short_name": _strip_prefix(name, settings.shells_tmux_session_prefix),
                    "project_dir": "",
                    "host": socket.gethostname(),
                    "created_at": now,
                    "status": "active",
                    "tags": [],
                },
            },
            upsert=True,
            return_document=True,
        )

        previous = int(doc.get("line_count", 0)) - count
        start_line = max(previous, 0) + 1

        docs: list[dict] = []
        has_output = False
        has_input = False
        for i, e in enumerate(events):
            docs.append(
                {
                    "shell_name": name,
                    "ts": now,
                    "line_number": start_line + i,
                    "kind": e.get("kind", "output"),
                    "text_raw": e.get("text_raw", ""),
                    "text_clean": e.get("text_clean", strip_ansi(e.get("text_raw", ""))),
                    "source": e.get("source", "pipe-pane"),
                    "byte_offset": e.get("byte_offset"),
                }
            )
            if docs[-1]["kind"] == "output":
                has_output = True
            if docs[-1]["kind"] == "input":
                has_input = True

        await self.events.insert_many(docs)

        if update_shell_timestamps:
            stamp: dict = {}
            if has_output:
                stamp["last_output_at"] = now
            if has_input:
                stamp["last_input_at"] = now
            if stamp:
                await self.shells.update_one({"name": name}, {"$set": stamp})

        return count

    async def insert_snapshot(
        self, name: str, content: str, content_hash: str
    ) -> None:
        shell = await self.get_shell(name)
        doc = {
            "shell_name": name,
            "ts": _utcnow(),
            "content": content,
            "content_hash": content_hash,
            "line_count_at_snapshot": shell.line_count if shell else 0,
        }
        await self.snapshots.insert_one(doc)

    async def capture_and_snapshot(self, name: str) -> Optional[ShellSnapshot]:
        """Run tmux capture-pane for a session and upsert a snapshot if it
        differs from the last. Returns the new snapshot or None if unchanged.
        """
        try:
            raw = await self.tmux.capture_pane(name, lines=settings.shells_snapshot_lines)
        except TmuxSessionNotFoundError:
            await self.mark_stopped(name)
            return None
        clean = strip_ansi(raw)
        h = hashlib.sha256(clean.encode("utf-8", errors="replace")).hexdigest()
        last = await self.get_last_snapshot(name)
        if last and last.content_hash == h:
            return None
        await self.insert_snapshot(name, clean, h)
        return await self.get_last_snapshot(name)

    # -------------------------------------------------------------- reconcile

    async def reconcile_statuses(self) -> None:
        """Mark shells as stopped if their tmux session no longer exists."""
        known = await self.list_shells(status=["active", "idle", "unknown"])
        for shell in known:
            try:
                alive = await self.tmux.has_session(shell.name)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("reconcile: has-session failed for %s: %s", shell.name, exc)
                continue
            if not alive:
                await self.mark_stopped(shell.name)

    async def discover_existing(self) -> int:
        """Register any already-running tmux sessions matching the prefix."""
        prefix = settings.shells_tmux_session_prefix
        try:
            names = await self.tmux.list_sessions(prefix=prefix)
        except Exception as exc:
            logger.debug("discover: list-sessions failed: %s", exc)
            return 0
        count = 0
        for name in names:
            await self.register_shell(name)
            count += 1
        return count

    # ----------------------------------------------------------------- stream

    async def stream_events(
        self,
        name: str,
        *,
        since_line: Optional[int] = None,
        poll_interval: float = 0.5,
    ) -> AsyncIterator[ShellEvent]:
        """Yield new events for a shell as they arrive.

        Simple polling implementation. Change streams are a future
        optimization — polling is resilient and predictable.
        """
        last_line = since_line or 0
        while True:
            batch = await self.list_events(
                name, since_line=last_line, limit=200, sort=1
            )
            for evt in batch:
                if evt.line_number > last_line:
                    last_line = evt.line_number
                yield evt
            await asyncio.sleep(poll_interval)

    # ------------------------------------------------------------------ input

    async def send_input(
        self,
        name: str,
        text: str,
        *,
        append_enter: bool = True,
        literal: bool = False,
    ) -> int:
        """Dispatch text to a tmux session and log an input event.

        Returns the line_number of the recorded input event.
        """
        shell = await self.get_shell(name)
        if not shell:
            raise ShellNotFoundError(name)
        if shell.status == "stopped":
            raise ShellStoppedError(name)
        try:
            await self.tmux.send_keys(
                name, text, append_enter=append_enter, literal=literal
            )
        except TmuxSessionNotFoundError as exc:
            await self.mark_stopped(name)
            raise ShellStoppedError(name) from exc

        await self.insert_events_batch(
            name,
            [
                {
                    "kind": "input",
                    "text_raw": text,
                    "text_clean": text,
                    "source": "send-keys",
                }
            ],
        )
        shell = await self.get_shell(name)
        return shell.line_count if shell else 0
