"""
ARIA - Watched Shells Service

Purpose: Business logic for shells — registration, event insertion, reads,
streaming, status reconciliation, and input dispatch.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Iterable, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.shells.ansi import matches_prompt, parse_prompt_patterns, strip_ansi
from aria.shells.claude_trust import ensure_trusted
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

    # ------------------------------------------------------- host awareness
    # A shell whose `host` names another machine is driven via the node command
    # queue rather than local tmux. The read path (scrollback, overview) is
    # host-agnostic; only the live ops (send_input/current_screen/liveness) and
    # kill dispatch by host.

    @staticmethod
    def _shell_is_remote(shell) -> bool:
        from aria.nodes import is_remote_host
        return is_remote_host(getattr(shell, "host", None))

    async def _node_online(self, node_id: str) -> bool:
        doc = await self.db.nodes.find_one({"_id": node_id})
        if not doc:
            return False
        hb = doc.get("last_heartbeat_at")
        if not hb:
            return False
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        return (_utcnow() - hb).total_seconds() < settings.node_heartbeat_timeout_seconds

    async def _remote_command(
        self, node_id: str, kind: str, args: dict, *, timeout: Optional[int] = None
    ) -> Optional[dict]:
        """Enqueue a command to a remote node and await its result. Returns the
        result dict, or None if the node is offline / the command times out."""
        from aria.nodes import commands
        if not await self._node_online(node_id):
            return None
        cmd_id = await commands.enqueue_command(self.db, node_id, kind, args)
        doc = await commands.await_result(self.db, cmd_id, timeout_seconds=timeout)
        if not doc or doc.get("status") != "done":
            return None
        return doc.get("result") or {}

    async def session_alive(self, name: str) -> bool:
        """Host-aware liveness: local → tmux has_session; remote → node online
        and the shell not marked stopped."""
        shell = await self.get_shell(name)
        if not shell:
            return False
        if self._shell_is_remote(shell):
            return shell.status != "stopped" and await self._node_online(shell.host)
        try:
            return await self.tmux.has_session(name)
        except Exception:  # pragma: no cover - defensive
            return False

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
        host: Optional[str] = None,
    ) -> Shell:
        """Create or refresh a shells doc for a tmux session.

        Idempotent — called on session-created and client-attached hooks. `host`
        defaults to this machine; the node-ingest path passes the remote node id
        so a remote shell is attributed to the machine it actually runs on.
        """
        now = _utcnow()
        prefix = settings.shells_tmux_session_prefix
        short = _strip_prefix(name, prefix)
        host = host or socket.gethostname()

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
        launch_command: Optional[str] = None,
        cols: Optional[int] = None,
        rows: Optional[int] = None,
    ) -> Shell:
        """Create a detached tmux session and register it as a watched shell.

        If the tmux session already exists, reclaim it unless Aria already
        tracks it as active/idle (true duplicate). Reclaim = register a
        missing Aria row, or reactivate a stopped one.

        `launch_command` runs an arbitrary command in the session instead of the
        default Claude launch command (used by the coding-session manager to run
        a coding agent on the watched-shell substrate). When set, it takes
        precedence over `launch_claude`.

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

        if launch_command:
            command = launch_command
        elif launch_claude:
            command = settings.shells_claude_launch_command
        else:
            command = None
        if command and settings.shells_claude_autotrust:
            # Pre-trust the workdir so Claude Code's blocking folder-trust
            # dialog doesn't hang the detached session. Best-effort, off the
            # event loop; failure just falls back to the old (dialog) behaviour.
            await asyncio.to_thread(ensure_trusted, workdir or None)
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
        row is still marked stopped. For a remote shell, dispatches a stop
        command to the owning node.
        """
        shell = await self.get_shell(name)
        if shell and self._shell_is_remote(shell):
            await self._remote_command(shell.host, "stop", {"name": name}, timeout=15)
            await self.mark_stopped(name)
            return
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
        host: Optional[str] = None,
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
        # Build the update conditionally: an empty "$set" ({}) is rejected by
        # MongoDB, so only include it when we actually have a field to set
        # (the stop path passes update_shell_timestamps=False).
        update: dict = {
            "$inc": {"line_count": count},
            "$setOnInsert": {
                "short_name": _strip_prefix(name, settings.shells_tmux_session_prefix),
                "project_dir": "",
                "host": host or socket.gethostname(),
                "created_at": now,
                "status": "active",
                "tags": [],
            },
        }
        if update_shell_timestamps:
            update["$set"] = {"last_activity_at": now}
        doc = await self.shells.find_one_and_update(
            {"name": name},
            update,
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

    # ---------------------------------------------------------------- overview

    async def current_screen(self, name: str, *, lines: int = 40) -> Optional[str]:
        """Return the live, ANSI-stripped visible pane of a shell.

        Unlike `get_last_snapshot`, this captures fresh (no DB round-trip) and
        does not persist. Returns None if the tmux session is gone (and marks
        the shell stopped). Intended for "what's on screen right now" reads by
        agents after sending input.

        For a remote shell, returns the latest snapshot the node pushed (it
        captures locally and streams snapshots) rather than a live pane.
        """
        shell = await self.get_shell(name)
        if shell and self._shell_is_remote(shell):
            snap = await self.get_last_snapshot(name)
            return snap.content if snap else None
        try:
            raw = await self.tmux.capture_pane(name, lines=lines)
        except TmuxSessionNotFoundError:
            await self.mark_stopped(name)
            return None
        return strip_ansi(raw).rstrip()

    async def fleet_overview(
        self,
        *,
        statuses: Iterable[str] = ("active", "idle"),
        tail_lines: int = 6,
    ) -> list[dict]:
        """Return an enriched, agent-friendly digest of watched shells.

        One call per shell does a short tail to compute the last visible line
        and whether the shell is sitting at an interactive prompt awaiting
        input. `awaiting_input` mirrors the IdleNotifier's logic exactly (idle
        past the threshold + last output line matches a prompt pattern) so the
        overview agrees with what actually fires notifications.

        `activity_state` is a richer four-way read on top of that (working /
        blocked / done / idle, inspired by herdr.dev's semantic state model):
        working = recent output; blocked = idle at a prompt (same signal as
        awaiting_input); done = idle AND this shell backs an ARIA coding
        session that reached a terminal status (completed/failed/stopped);
        idle = idle with neither. "done" only applies to coding-session-backed
        shells — a hand-run shell has no such oracle, and only really shows up
        here in practice for the brief window before a finished session's
        shell gets `mark_stopped()`'d and drops out of the default view.
        """
        patterns = parse_prompt_patterns(settings.shells_idle_prompt_patterns)
        idle_threshold = int(settings.shells_idle_threshold_seconds or 60)
        now = _utcnow()

        shells = await self.list_shells(status=list(statuses))
        session_status_by_shell: dict[str, str] = {}
        if shells:
            names = [s.name for s in shells]
            async for doc in self.db.coding_sessions.find(
                {"shell_name": {"$in": names}}, {"shell_name": 1, "status": 1}
            ):
                shell_name = doc.get("shell_name")
                if shell_name:
                    session_status_by_shell[shell_name] = doc.get("status")

        out: list[dict] = []
        for shell in shells:
            la = shell.last_activity_at
            if la.tzinfo is None:
                la = la.replace(tzinfo=timezone.utc)
            idle_seconds = max(0, int((now - la).total_seconds()))

            tail = await self.tail(shell.name, lines=tail_lines)
            # Prefer the most recent line with real (alphanumeric) content so a
            # trailing spinner frame or stray escape leftover doesn't become the
            # summary. Re-strip stored text in case it predates the ansi fixes.
            last_line = None
            fallback_line = None
            for ev in reversed(tail):
                text = strip_ansi(ev.text_clean).strip()
                if not text:
                    continue
                if fallback_line is None:
                    fallback_line = text
                if any(c.isalnum() for c in text):
                    last_line = text
                    break
            last_line = last_line or fallback_line

            awaiting = False
            prompt_line = None
            if tail and idle_seconds >= idle_threshold:
                last = tail[-1]
                if last.kind == "output" and matches_prompt(last.text_clean, patterns):
                    awaiting = True
                    prompt_line = last.text_clean.strip()[:200]

            if idle_seconds < idle_threshold:
                activity_state = "working"
            elif session_status_by_shell.get(shell.name) in ("completed", "failed", "stopped"):
                activity_state = "done"
            elif awaiting:
                activity_state = "blocked"
            else:
                activity_state = "idle"

            out.append(
                {
                    "name": shell.name,
                    "short_name": shell.short_name,
                    "status": shell.status,
                    "activity_state": activity_state,
                    "host": shell.host,
                    "project_dir": shell.project_dir,
                    "line_count": shell.line_count,
                    "last_activity_at": la,
                    "idle_seconds": idle_seconds,
                    "awaiting_input": awaiting,
                    "prompt_line": prompt_line,
                    "last_line": (last_line or "")[:200],
                    "tags": shell.tags,
                }
            )

        # Surface shells that need attention first: blocked/awaiting input,
        # then done (a finished task is worth a glance too), then most
        # recently active (smallest idle_seconds) ahead of plain idle ones.
        _STATE_ORDER = {"blocked": 0, "done": 1, "working": 2, "idle": 3}
        out.sort(key=lambda s: (_STATE_ORDER.get(s["activity_state"], 9), s["idle_seconds"]))
        return out

    # -------------------------------------------------------------- reconcile

    async def reconcile_statuses(self) -> None:
        """Mark shells as stopped if their tmux session no longer exists."""
        known = await self.list_shells(status=["active", "idle", "unknown"])
        for shell in known:
            if self._shell_is_remote(shell):
                continue  # a remote node owns its own shells' liveness
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

    # --------------------------------------------------------------- adoption

    @staticmethod
    def _capture_pidfile(name: str) -> str:
        return f"/tmp/aria-shell-capture-{name}.pid"

    def _capture_alive(self, name: str) -> bool:
        """True if a capture process for `name` is recorded and still running.

        Mirrors scripts/aria-shell-register so the in-process reconciler and the
        tmux-hook path agree on whether capture is active (shared pidfile)."""
        try:
            with open(self._capture_pidfile(name)) as fh:
                pid = int(fh.read().strip())
        except (FileNotFoundError, ValueError):
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    async def ensure_capture(self, name: str) -> bool:
        """Start pipe-pane capture for `name` if it isn't already running.

        Idempotent backstop for the tmux session-created/client-attached hooks:
        the capture shim writes the pidfile we check here, so we never start a
        second pipe (pipe-pane -o would otherwise toggle an active one off).
        Returns True if capture was (re)started, False if already alive."""
        if self._capture_alive(name):
            return False
        shim = settings.shells_capture_shim
        try:
            await self.tmux.pipe_pane(name, f"{shim} {name}")
            logger.info("adopt: started capture for %s", name)
            return True
        except TmuxError as exc:
            logger.warning("adopt: could not start capture for %s: %s", name, exc)
            return False

    async def adopt_session(self, name: str, *, project_dir: str = "") -> Shell:
        """Adopt an externally-started tmux session: register it (idempotent)
        and ensure capture is running. Used by the poll reconciler and can be
        called directly to pick up a session started outside ProjectAria."""
        shell = await self.register_shell(name, project_dir=project_dir)
        await self.ensure_capture(name)
        return shell

    async def reconcile_adopt(self) -> int:
        """Discover live `claude-*` sessions and ensure each is registered and
        captured. Backstop for sessions the tmux hook missed (started before the
        hook was installed, or whose capture process died). Returns the count of
        sessions for which capture was (re)started."""
        prefix = settings.shells_tmux_session_prefix
        try:
            names = await self.tmux.list_sessions(prefix=prefix)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("reconcile_adopt: list-sessions failed: %s", exc)
            return 0
        started = 0
        for name in names:
            try:
                await self.register_shell(name)
                if await self.ensure_capture(name):
                    started += 1
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("reconcile_adopt: %s failed: %s", name, exc)
        return started

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
        wait_ms: int = 0,
    ) -> tuple[int, Optional[str]]:
        """Dispatch text to a tmux session and log an input event.

        Returns ``(line_number, screen)``. When ``wait_ms`` > 0, sleep that
        long after sending and capture the resulting visible pane so the caller
        can observe the effect of its input in a single round-trip; otherwise
        ``screen`` is None. This closes the act→observe loop for agents like
        Hermes that would otherwise send-then-poll.
        """
        shell = await self.get_shell(name)
        if not shell:
            raise ShellNotFoundError(name)

        # Remote shell: dispatch to the owning node and await its result. The
        # node runs send-keys locally, then (if wait_ms) captures and returns
        # the screen. line>0 signals success; the node echoes the input line
        # back through its normal event stream. We DON'T gate on the cached
        # status here — the node is authoritative on liveness, and the captured
        # status can transiently flap to 'stopped'; the node returns a failure
        # if the session is really gone.
        if self._shell_is_remote(shell):
            result = await self._remote_command(
                shell.host,
                "send_input",
                {
                    "name": name,
                    "text": text,
                    "append_enter": append_enter,
                    "literal": literal,
                    "wait_ms": wait_ms,
                },
                timeout=max(settings.node_command_timeout_seconds, wait_ms // 1000 + 5),
            )
            if result is None:
                return (0, None)  # node offline / timed out — not sent
            return (int(result.get("line") or 1), result.get("screen"))

        # Local shell: tmux is authoritative right here, so honor a stopped row.
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
        line = shell.line_count if shell else 0

        screen: Optional[str] = None
        if wait_ms and wait_ms > 0:
            await asyncio.sleep(min(wait_ms, 10000) / 1000.0)
            try:
                screen = await self.current_screen(name)
            except TmuxError as exc:  # pragma: no cover - defensive
                logger.debug("send_input: post-capture failed for %s: %s", name, exc)
        return line, screen
