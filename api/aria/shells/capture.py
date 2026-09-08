"""
ARIA - Watched Shells Capture

Purpose: Drain a `tmux pipe-pane` byte stream independently of MongoDB
writes and send nonblocking hints to active screen viewers.

Invocation (from tmux hook):
    python3 -m aria.shells.capture <shell_name>

Must never crash — tmux would close the pipe if this process dies.
Reconnect to Mongo with backoff, buffer in memory, drop oldest on overflow.
"""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
import logging
import os
import signal
import sys

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import BulkWriteError

from aria.shells.ansi import strip_ansi
from aria.shells.screen_stream import ScreenNotifier

logger = logging.getLogger("aria.shells.capture")


class CaptureBuffer:
    """Drop oldest operational history at a fixed byte/count budget."""
    def __init__(self, max_bytes=4 * 1024 * 1024, max_items=10000):
        self.records = deque()
        self.bytes = 0
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.dropped = 0
        self.ready = asyncio.Event()

    def append(self, record):
        size = len(record["text_raw"].encode()) + len(record["text_clean"].encode())
        self.records.append((record, size))
        self.bytes += size
        while self.bytes > self.max_bytes or len(self.records) > self.max_items:
            _, removed = self.records.popleft()
            self.bytes -= removed
            self.dropped += 1
        self.ready.set()

    def take(self, count):
        batch = []
        while self.records and len(batch) < count:
            record, size = self.records.popleft()
            self.bytes -= size
            batch.append(record)
        if not self.records:
            self.ready.clear()
        return batch


async def drain_output(reader, buffer, shell_name, notifier):
    """Read independently of Mongo writes/retries so capture cannot stall tmux."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await reader.read(64 * 1024)
        raw = decoder.decode(chunk, final=not chunk)
        if raw:
            buffer.append({"shell_name": shell_name, "kind": "output",
                           "text_raw": raw, "text_clean": strip_ansi(raw).rstrip("\n"),
                           "source": "pipe-pane"})
            notifier.notify(shell_name)
        if not chunk:
            return


async def persist_batch(shells, events, shell_name, batch):
    if "line_number" not in batch[0]:
        now_utc = _utcnow()
        doc = await shells.find_one_and_update(
            {"name": shell_name},
            {
                "$inc": {"line_count": len(batch)},
                "$set": {"last_activity_at": now_utc, "last_output_at": now_utc},
            },
            upsert=False,
            return_document=True,
        )
        # Registration belongs to the API/adopter. A final EOF flush after
        # purge must not recreate the removed shell row.
        if doc is None:
            return
        previous = int(doc.get("line_count", 0)) - len(batch)
        start_line = max(previous, 0) + 1
        for i, rec in enumerate(batch):
            rec["line_number"] = start_line + i
            rec["ts"] = now_utc
    try:
        await events.insert_many(batch, ordered=False)
    except BulkWriteError as exc:
        # Retried documents keep Mongo _ids and line numbers. Duplicates
        # alone mean the preceding timed-out write actually succeeded.
        if exc.details.get("writeConcernErrors") or any(
            err.get("code") != 11000 for err in exc.details.get("writeErrors", [])
        ):
            raise


async def _run_capture(shell_name: str) -> None:
    mongo_url = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/?directConnection=true&replicaSet=rs0")
    client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=3000)
    db = client[os.environ.get("MONGODB_DATABASE", "aria")]
    shells, events = db.shells, db.shell_events
    flush_interval = max(0.05, int(os.environ.get("SHELLS_CAPTURE_FLUSH_MS", "500")) / 1000)
    batch_size = max(1, int(os.environ.get("SHELLS_CAPTURE_BATCH_SIZE", "50")))
    buffer = CaptureBuffer(max_items=int(os.environ.get("SHELLS_CAPTURE_MAX_BUFFER", "10000")))
    notifier = ScreenNotifier()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    idle = asyncio.Event()
    idle.set()

    async def writer():
        backoff = 1.0
        while True:
            await buffer.ready.wait()
            await asyncio.sleep(flush_interval)
            idle.clear()
            batch = buffer.take(batch_size)
            if not batch:
                continue
            # Keep only one bounded batch in flight; input drains concurrently.
            while True:
                try:
                    await asyncio.wait_for(persist_batch(shells, events, shell_name, batch), timeout=5)
                    backoff = 1.0
                    idle.set()
                    break
                except Exception as exc:
                    logger.warning("capture: persistence retry (%s); dropped=%d", type(exc).__name__, buffer.dropped)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)

    drain = asyncio.create_task(drain_output(reader, buffer, shell_name, notifier))
    write = asyncio.create_task(writer())
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([drain, stopping], return_when=asyncio.FIRST_COMPLETED)
        if drain in done:
            await drain
            # A short EOF grace drains normal batches, but never waits forever
            # for a database that is down during a capture upgrade/shutdown.
            deadline = loop.time() + 2
            while (buffer.records or not idle.is_set()) and loop.time() < deadline:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.1)
    finally:
        for task in (drain, write, stopping):
            task.cancel()
        await asyncio.gather(drain, write, stopping, return_exceptions=True)
        transport.close()
        notifier.close()
        client.close()
        logger.info("capture: exiting shell=%s dropped=%d", shell_name, buffer.dropped)


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def main() -> None:  # pragma: no cover - CLI entry
    if len(sys.argv) < 2:
        print("usage: python -m aria.shells.capture <shell_name>", file=sys.stderr)
        sys.exit(2)
    shell_name = sys.argv[1]
    logging.basicConfig(
        level=os.environ.get("SHELLS_CAPTURE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(_run_capture(shell_name))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
