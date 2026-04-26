"""
ARIA - Watched Shells tmux client

Purpose: Thin async wrapper around the tmux CLI. Read-mostly — ARIA does not
own watched sessions, it only observes and relays input to them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TmuxError(RuntimeError):
    """Raised when a tmux command fails unexpectedly."""


class TmuxSessionNotFoundError(TmuxError):
    """Raised when a tmux session does not exist."""


class TmuxClient:
    """Async wrapper around `tmux` subprocess calls.

    The tmux CLI is assumed to be available on PATH on the same host as the
    API process. All methods are safe to call concurrently.
    """

    def __init__(self, tmux_binary: str = "tmux"):
        self.tmux_binary = tmux_binary

    async def _run(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            self.tmux_binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def list_sessions(self, prefix: Optional[str] = None) -> list[str]:
        """List all tmux session names. Optionally filter by prefix."""
        rc, out, err = await self._run("list-sessions", "-F", "#{session_name}")
        if rc != 0:
            # No server running is not an error
            if "no server running" in err.lower() or "error connecting" in err.lower():
                return []
            raise TmuxError(f"tmux list-sessions failed: {err.strip()}")
        names = [line.strip() for line in out.splitlines() if line.strip()]
        if prefix:
            names = [n for n in names if n.startswith(prefix)]
        return names

    async def has_session(self, name: str) -> bool:
        rc, _out, _err = await self._run("has-session", "-t", name)
        return rc == 0

    async def send_keys(
        self,
        name: str,
        text: str,
        *,
        append_enter: bool = True,
        literal: bool = False,
    ) -> None:
        """Send input to a tmux session.

        When `literal` is True, `-l` is passed to tmux so key names like
        `Enter` are sent as characters rather than interpreted. `append_enter`
        adds a trailing `Enter` keystroke (submission) when not in literal mode.
        """
        if not await self.has_session(name):
            raise TmuxSessionNotFoundError(name)
        args: list[str] = ["send-keys", "-t", name]
        if literal:
            args.append("-l")
        args.append(text)
        if append_enter and not literal:
            args.append("Enter")
        rc, _out, err = await self._run(*args)
        if rc != 0:
            raise TmuxError(f"tmux send-keys failed: {err.strip()}")

    async def capture_pane(self, name: str, *, lines: int = 10000) -> str:
        """Capture the current pane contents for a session.

        Uses `-p` (print), `-S -<lines>` (start from N lines back),
        and `-J` (join wrapped lines).
        """
        if not await self.has_session(name):
            raise TmuxSessionNotFoundError(name)
        rc, out, err = await self._run(
            "capture-pane",
            "-p",
            "-J",
            "-S",
            f"-{int(lines)}",
            "-t",
            name,
        )
        if rc != 0:
            raise TmuxError(f"tmux capture-pane failed: {err.strip()}")
        return out

    async def kill_session(self, name: str) -> None:
        rc, _out, err = await self._run("kill-session", "-t", name)
        if rc != 0 and "session not found" not in err.lower():
            raise TmuxError(f"tmux kill-session failed: {err.strip()}")

    async def new_session(
        self,
        name: str,
        *,
        workdir: Optional[str] = None,
        command: Optional[str] = None,
        cols: Optional[int] = None,
        rows: Optional[int] = None,
    ) -> None:
        """Create a detached tmux session.

        If `command` is provided, it runs in the session shell (and the
        session exits when it finishes). If `workdir` is provided it sets
        the session's starting directory. `cols` / `rows` set the initial
        window geometry — tmux defaults to 80x24, which is too narrow for
        most TUIs (Claude Code, htop). Pass at least 120x40 unless the
        client knows its real size.
        """
        args: list[str] = ["new-session", "-d", "-s", name]
        if cols and rows:
            args += ["-x", str(int(cols)), "-y", str(int(rows))]
        if workdir:
            args += ["-c", workdir]
        if command:
            args.append(command)
        rc, _out, err = await self._run(*args)
        if rc != 0:
            raise TmuxError(f"tmux new-session failed: {err.strip()}")

    async def resize_window(self, name: str, cols: int, rows: int) -> None:
        """Resize a session's active window to the given geometry.

        Sends SIGWINCH to processes in the pane, so TUIs (Claude Code, vim,
        htop) repaint at the new size. tmux silently caps the size to what
        the smallest attached client supports — for our use case there are
        no live tmux clients (sessions are detached), so the size sticks.
        """
        if not await self.has_session(name):
            raise TmuxSessionNotFoundError(name)
        rc, _out, err = await self._run(
            "resize-window", "-t", name, "-x", str(int(cols)), "-y", str(int(rows))
        )
        if rc != 0:
            raise TmuxError(f"tmux resize-window failed: {err.strip()}")
