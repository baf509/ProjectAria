"""
ARIA - Watched Shells Capture

Purpose: Entry point for a `tmux pipe-pane` subprocess. Reads stdin
line-by-line, batches events, and writes them to MongoDB.

Invocation (from tmux hook):
    python3 -m aria.shells.capture <shell_name>

Must never crash — tmux would close the pipe if this process dies.
Reconnect to Mongo with backoff, buffer in memory, drop oldest on overflow.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

from aria.shells.ansi import strip_ansi

logger = logging.getLogger("aria.shells.capture")


async def _run_capture(shell_name: str) -> None:
    mongo_url = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/?directConnection=true&replicaSet=rs0")
    mongo_db = os.environ.get("MONGODB_DATABASE", "aria")
    flush_ms = int(os.environ.get("SHELLS_CAPTURE_FLUSH_MS", "500"))
    batch_size = int(os.environ.get("SHELLS_CAPTURE_BATCH_SIZE", "50"))
    max_buffer = int(os.environ.get("SHELLS_CAPTURE_MAX_BUFFER", "10000"))

    client = AsyncIOMotorClient(mongo_url)
    db = client[mongo_db]
    shells = db.shells
    events = db.shell_events

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_event_loop()
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    pending: list[dict] = []
    last_flush = time.monotonic()
    flush_interval = flush_ms / 1000.0
    backoff = 1.0

    stop = asyncio.Event()

    def _handle_signal(*_args):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:  # pragma: no cover
            pass

    logger.info("capture: starting shell=%s flush=%sms batch=%d", shell_name, flush_ms, batch_size)

    async def flush() -> None:
        nonlocal pending, last_flush, backoff
        if not pending:
            last_flush = time.monotonic()
            return
        batch = pending
        pending = []
        try:
            now_utc = _utcnow()
            doc = await shells.find_one_and_update(
                {"name": shell_name},
                {
                    "$inc": {"line_count": len(batch)},
                    "$set": {"last_activity_at": now_utc, "last_output_at": now_utc},
                    "$setOnInsert": {
                        "short_name": shell_name.split("-", 1)[-1] if "-" in shell_name else shell_name,
                        "project_dir": os.environ.get("SHELLS_CAPTURE_PROJECT_DIR", ""),
                        "host": os.uname().nodename if hasattr(os, "uname") else "",
                        "created_at": now_utc,
                        "status": "active",
                        "tags": [],
                    },
                },
                upsert=True,
                return_document=True,
            )
            previous = int(doc.get("line_count", 0)) - len(batch)
            start_line = max(previous, 0) + 1
            for i, rec in enumerate(batch):
                rec["line_number"] = start_line + i
                rec["ts"] = now_utc
            await events.insert_many(batch)
            last_flush = time.monotonic()
            backoff = 1.0
        except Exception as exc:
            logger.warning("capture: flush failed (%s), buffering and retrying", exc)
            # Re-queue at the front, drop oldest on overflow.
            pending = batch + pending
            if len(pending) > max_buffer:
                drop = len(pending) - max_buffer
                pending = pending[drop:]
                logger.warning("capture: buffer overflow, dropped %d events", drop)
            await asyncio.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2, 30.0)

    try:
        while not stop.is_set():
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=flush_interval)
            except asyncio.TimeoutError:
                line = b""
            if line:
                raw = line.decode("utf-8", errors="replace").rstrip("\n")
                pending.append(
                    {
                        "shell_name": shell_name,
                        "kind": "output",
                        "text_raw": raw,
                        "text_clean": strip_ansi(raw),
                        "source": "pipe-pane",
                    }
                )
            else:
                # EOF on stdin → tmux closed the pipe
                if reader.at_eof():
                    break
            now = time.monotonic()
            if pending and (len(pending) >= batch_size or now - last_flush >= flush_interval):
                await flush()
    finally:
        await flush()
        client.close()
        logger.info("capture: exiting shell=%s", shell_name)


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
