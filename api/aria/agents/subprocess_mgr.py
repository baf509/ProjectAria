"""
ARIA - Coding Subprocess Manager

Purpose: Spawn and monitor coding agent subprocesses.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from aria.agents.backends.base import CommandSpec
from aria.config import settings


@dataclass
class RunningProcess:
    process: asyncio.subprocess.Process
    stdout_lines: deque[str] = field(default_factory=lambda: deque(maxlen=settings.coding_output_lines))
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=settings.coding_output_lines))
    readers: list[asyncio.Task] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class CodingSubprocessManager:
    """Manage coding agent subprocesses and buffered output."""

    def __init__(self):
        self._processes: dict[str, RunningProcess] = {}

    async def spawn(self, session_id: str, command: CommandSpec) -> RunningProcess:
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=command.cwd,
            env=command.env or None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        running = RunningProcess(process=process)
        self._processes[session_id] = running
        running.readers = [
            asyncio.create_task(self._read_stream(running.stdout_lines, process.stdout)),
            asyncio.create_task(self._read_stream(running.stderr_lines, process.stderr)),
        ]
        return running

    async def send_input(self, session_id: str, text: str) -> bool:
        running = self._processes.get(session_id)
        if running is None or running.process.stdin is None:
            return False
        if running.process.returncode is not None:
            return False
        try:
            running.process.stdin.write(text.encode("utf-8"))
            if not text.endswith("\n"):
                running.process.stdin.write(b"\n")
            await running.process.stdin.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            return False
        return True

    async def stop(self, session_id: str) -> bool:
        running = self._processes.get(session_id)
        if running is None:
            return False
        if running.process.returncode is None:
            running.process.terminate()
            try:
                await asyncio.wait_for(running.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                running.process.kill()
                await running.process.wait()
        for reader in running.readers:
            if not reader.done():
                reader.cancel()
        self._processes.pop(session_id, None)
        return True

    async def wait(self, session_id: str) -> Optional[int]:
        running = self._processes.get(session_id)
        if running is None:
            return None
        return await running.process.wait()

    def get_output(self, session_id: str, lines: int = 50) -> str:
        running = self._processes.get(session_id)
        if running is None:
            return ""
        combined = list(running.stdout_lines) + list(running.stderr_lines)
        return "\n".join(combined[-lines:])

    def get_pid(self, session_id: str) -> Optional[int]:
        running = self._processes.get(session_id)
        if running is None:
            return None
        return running.process.pid

    def is_running(self, session_id: str) -> bool:
        running = self._processes.get(session_id)
        return bool(running and running.process.returncode is None)

    async def _read_stream(self, buffer: deque[str], stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            buffer.append(line.decode("utf-8", errors="replace").rstrip())
