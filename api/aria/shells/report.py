"""Weekly heartbeat report.

A periodic "all good" summary texted via Signal so silence is never ambiguous
(healthy vs. the monitor itself being dead). Sent once on the configured weekday
once the hour is reached; a Mongo marker prevents duplicate sends across restarts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


async def build_report(db) -> dict:
    """Assemble the weekly summary. Returns {text, stats}."""
    from aria.shells.selfcheck import run_checks

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    total_p = await db.projects.count_documents({})
    active_p = await db.projects.count_documents({"activity_status": "active"})
    open_tasks = await db.tasks.count_documents({"status": {"$ne": "done"}})
    mem_total = await db.memories.estimated_document_count()
    mem_week = await db.memories.count_documents({"created_at": {"$gte": week_ago}})
    events = await db.shell_events.estimated_document_count()

    checks = await run_checks(db)
    svc = " ".join(f"{c['name']}{'✓' if c['ok'] else '✗'}" for c in checks)
    all_ok = all(c["ok"] for c in checks)

    top = []
    async for p in db.projects.find({"activity_status": "active"}).sort("last_activity_at", -1).limit(3):
        top.append(p.get("slug") or p.get("name"))

    text = (
        f"{'✅' if all_ok else '⚠️'} ARIA weekly report (corsair-ai)\n"
        f"Projects: {total_p} ({active_p} active, {total_p - active_p} idle)\n"
        f"Open tasks: {open_tasks}\n"
        f"Memories: {mem_total} (+{mem_week} this week)\n"
        f"Scrollback: {events:,} events\n"
        f"Services: {svc}"
    )
    if top:
        text += f"\nMost active: {', '.join(top)}"

    stats = {
        "projects": total_p, "active": active_p, "open_tasks": open_tasks,
        "memories": mem_total, "memories_week": mem_week, "events": events,
        "services_ok": all_ok, "top_active": top,
    }
    return {"text": text, "stats": stats}


class HeartbeatReportWorker:
    """Sends build_report() via Signal weekly, on `weekday` at/after `hour` (local time)."""

    def __init__(self, db, notifier, weekday: int, hour: int, interval_seconds: int = 1800):
        self.db = db
        self.notifier = notifier
        self.weekday = int(weekday)   # Mon=0 .. Sun=6 (datetime.weekday())
        self.hour = int(hour)
        self.interval = max(300, int(interval_seconds))
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="shells.report")
        logger.info("heartbeat report worker started (weekday=%d hour=%d)", self.weekday, self.hour)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def _maybe_send(self) -> bool:
        now = datetime.now()  # local (box is America/New_York)
        if now.weekday() != self.weekday or now.hour < self.hour:
            return False
        today = now.date().isoformat()
        state = await self.db.report_state.find_one({"_id": "heartbeat"})
        if state and state.get("last_date") == today:
            return False  # already sent today

        report = await build_report(self.db)
        if self.notifier:
            await self.notifier.notify(
                source="aria-shells", event_type="weekly report",
                detail=report["text"], cooldown_seconds=0,
            )
        await self.db.report_state.update_one(
            {"_id": "heartbeat"},
            {"$set": {"last_date": today, "sent_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        logger.info("weekly report sent")
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._maybe_send()
            except Exception as exc:  # pragma: no cover
                logger.warning("report tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
