"""Shared, latest-only visible screens; pipe-pane output is a best-effort wakeup.

No database access in the local refresh path. One reader per watched shell,
coalesced at 100 ms, and no screen capture without subscribers. Notifications
carry a name only; loss is repaired by a slow fallback capture.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import fcntl
import logging
import os
from pathlib import Path
import socket
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


def socket_path() -> Path:
    return Path(os.getenv("ARIA_SHELL_SCREEN_SOCKET", str(Path.home() / ".local/state/aria/shell-screen.sock")))


class ScreenNotifier:
    """Nonblocking hint sender. API downtime must never stall terminal output."""
    def __init__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.path = str(socket_path())

    def notify(self, name: str) -> None:
        try:
            self.sock.sendto(name.encode()[:256], self.path)
        except OSError:
            pass

    def close(self) -> None:
        self.sock.close()


@dataclass
class _Watch:
    fetch: Callable[[], Awaitable[str]]
    dirty: asyncio.Event = field(default_factory=asyncio.Event)
    queues: set = field(default_factory=set)
    latest: dict | None = None
    task: asyncio.Task | None = None


class _Hints(asyncio.DatagramProtocol):
    def __init__(self, hub):
        self.hub = hub

    def datagram_received(self, data, addr):
        if len(data) <= 256:
            self.hub.notify(data.decode("utf-8", errors="replace"))


class ScreenHub:
    def __init__(self, *, interval=0.1, fallback=2.0, path: Path | None = None):
        self.interval = interval
        self.fallback = fallback
        self.path = path or socket_path()
        self.watches: dict[str, _Watch] = {}
        self.transport = None
        self.lock = None
        self._start_lock = asyncio.Lock()

    async def start(self):
        async with self._start_lock:
            if self.transport is not None:
                return
            lock = None
            sock = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                lock = self.path.with_suffix(".lock").open("a")
                # A second API worker must not steal the first worker's socket.
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.path.unlink(missing_ok=True)
                # Bind explicitly: uvloop's local_addr accepts IP tuples only.
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                sock.setblocking(False)
                sock.bind(str(self.path))
                os.chmod(self.path, 0o600)
                transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: _Hints(self), sock=sock,
                )
                self.transport, self.lock = transport, lock
            except OSError:
                if sock:
                    sock.close()
                if lock:
                    lock.close()
                logger.debug("screen hints unavailable; using shared fallback captures")

    def notify(self, name: str):
        watch = self.watches.get(name)
        if watch:
            watch.dirty.set()

    @asynccontextmanager
    async def subscribe(self, name: str, fetch: Callable[[], Awaitable[str]]):
        await self.start()
        watch = self.watches.get(name)
        if watch is None:
            watch = self.watches[name] = _Watch(fetch)
        queue = asyncio.Queue(maxsize=1)
        watch.queues.add(queue)
        if watch.latest is not None:
            queue.put_nowait(watch.latest)
        if watch.task is None:
            watch.task = asyncio.create_task(self._read(name, watch))
        try:
            yield queue
        finally:
            watch.queues.discard(queue)
            if not watch.queues:
                self.watches.pop(name, None)
                watch.task.cancel()
                await asyncio.gather(watch.task, return_exceptions=True)

    async def _read(self, name, watch):
        while True:
            watch.dirty.clear()
            try:
                screen = await asyncio.wait_for(watch.fetch(), timeout=3)
                # Protect transport/client memory even if a remote snapshot is huge.
                screen = screen[-100_000:]
                payload = {"name": name, "screen": screen, "lines": len(screen.splitlines())}
            except Exception:
                payload = {"name": name, "error": "Screen unavailable; reconnecting"}
            if payload != watch.latest:
                watch.latest = payload
                for queue in tuple(watch.queues):
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(payload)
            try:
                await asyncio.wait_for(watch.dirty.wait(), self.fallback)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(self.interval)

    async def stop(self):
        tasks = [w.task for w in self.watches.values() if w.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.watches.clear()
        if self.transport:
            self.transport.close()
            self.transport = None
            self.path.unlink(missing_ok=True)
        if self.lock:
            self.lock.close()
            self.lock = None


screen_hub = ScreenHub()
