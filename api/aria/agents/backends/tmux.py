"""
ARIA - Tmux Agent Backend

Purpose: Spawn coding agents in visible tmux panes so the user can
watch multiple agents working in parallel. Each agent gets its own
pane in a dedicated ARIA tmux session.

Inspired by Claude Code's swarm tmux backend — gives sub-agents
a visible, color-coded presence in the terminal.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

TMUX_SESSION_NAME = "aria-agents"

# ANSI colors for pane borders — cycle through for visual distinction
_PANE_COLORS = ["red", "green", "blue", "yellow", "magenta", "cyan"]


@dataclass
class TmuxPane:
    pane_id: str
    session_id: str
    color: str


class TmuxManager:
    """Manage a tmux session with panes for sub-agent visibility."""

    def __init__(self, session_name: str = TMUX_SESSION_NAME):
        self.session_name = session_name
        self._panes: dict[str, TmuxPane] = {}  # session_id -> TmuxPane
        self._color_idx = 0

    @staticmethod
    def is_available() -> bool:
        """Check if tmux is installed."""
        return shutil.which("tmux") is not None

    async def _run_tmux(self, *args: str) -> tuple[int, str]:
        """Run a tmux command and return (exit_code, stdout)."""
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode("utf-8", errors="replace").strip()

    async def _ensure_session(self) -> None:
        """Create the tmux session if it doesn't exist."""
        code, _ = await self._run_tmux("has-session", "-t", self.session_name)
        if code != 0:
            await self._run_tmux(
                "new-session", "-d", "-s", self.session_name,
                "-x", "200", "-y", "50",
            )
            logger.info("Created tmux session: %s", self.session_name)

    def _next_color(self) -> str:
        color = _PANE_COLORS[self._color_idx % len(_PANE_COLORS)]
        self._color_idx += 1
        return color

    async def spawn_pane(
        self,
        session_id: str,
        command: str,
        title: Optional[str] = None,
    ) -> TmuxPane:
        """Spawn a new tmux pane running the given shell command.

        Args:
            session_id: ARIA coding session ID (for tracking).
            command: Full shell command to run in the pane.
            title: Optional pane title (shown in border).

        Returns:
            TmuxPane with the pane ID.
        """
        await self._ensure_session()
        color = self._next_color()

        # Split a new pane in the session
        code, pane_id = await self._run_tmux(
            "split-window", "-t", self.session_name,
            "-P", "-F", "#{pane_id}",
            "-h",  # horizontal split
            command,
        )
        if code != 0:
            # If split fails (only one pane), try sending to the first pane
            # by creating a new window instead
            code, pane_id = await self._run_tmux(
                "new-window", "-t", self.session_name,
                "-P", "-F", "#{pane_id}",
                command,
            )

        if not pane_id:
            raise RuntimeError(f"Failed to create tmux pane for session {session_id}")

        pane_id = pane_id.strip()

        # Set pane title and border color
        if title:
            await self._run_tmux(
                "select-pane", "-t", pane_id,
                "-T", title,
            )
        await self._run_tmux(
            "select-pane", "-t", pane_id,
            "-P", f"fg={color}",
        )

        # Rebalance pane layout
        await self._run_tmux("select-layout", "-t", self.session_name, "tiled")

        pane = TmuxPane(pane_id=pane_id, session_id=session_id, color=color)
        self._panes[session_id] = pane
        logger.info(
            "Spawned tmux pane %s for session %s (color=%s)",
            pane_id, session_id, color,
        )
        return pane

    async def kill_pane(self, session_id: str) -> bool:
        """Kill the tmux pane for a given session."""
        pane = self._panes.pop(session_id, None)
        if pane is None:
            return False
        code, _ = await self._run_tmux("kill-pane", "-t", pane.pane_id)
        return code == 0

    async def capture_output(self, session_id: str, lines: int = 50) -> str:
        """Capture recent output from a pane."""
        pane = self._panes.get(session_id)
        if pane is None:
            return ""
        _, output = await self._run_tmux(
            "capture-pane", "-t", pane.pane_id,
            "-p",  # print to stdout
            "-S", f"-{lines}",
        )
        return output

    async def list_panes(self) -> list[dict]:
        """List all active agent panes."""
        code, output = await self._run_tmux(
            "list-panes", "-t", self.session_name,
            "-F", "#{pane_id}|#{pane_title}|#{pane_active}|#{pane_width}x#{pane_height}",
        )
        if code != 0:
            return []

        panes = []
        pane_id_to_session = {p.pane_id: p.session_id for p in self._panes.values()}
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) >= 4:
                pane_id = parts[0]
                panes.append({
                    "pane_id": pane_id,
                    "title": parts[1],
                    "active": parts[2] == "1",
                    "size": parts[3],
                    "session_id": pane_id_to_session.get(pane_id),
                })
        return panes

    async def cleanup(self) -> None:
        """Kill the entire tmux session."""
        await self._run_tmux("kill-session", "-t", self.session_name)
        self._panes.clear()
        logger.info("Cleaned up tmux session: %s", self.session_name)
