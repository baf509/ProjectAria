"""Self-monitoring + alerting for the aria-shells stack.

Periodically verifies the things that silently broke before (a dead model
endpoint, stalled extraction, an unreachable DB) and pushes a Signal alert via
the existing NotificationService when something is wrong — with a cooldown so a
sustained outage doesn't spam. A recovery notice is sent once when it clears.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from aria.config import settings

logger = logging.getLogger(__name__)


async def _check_http(url: str, timeout: float = 4.0) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url)
        # <500 means the service is up and answering (404 on a probe path is fine)
        return (r.status_code < 500, f"HTTP {r.status_code}")
    except Exception as exc:
        return (False, type(exc).__name__)


async def run_checks(db) -> list[dict]:
    """Return a list of {name, ok, detail} for each monitored dependency."""
    checks: list[dict] = []

    # MongoDB
    try:
        await db.command("ping")
        checks.append({"name": "mongodb", "ok": True, "detail": "ping ok"})
    except Exception as exc:
        checks.append({"name": "mongodb", "ok": False, "detail": str(exc)[:120]})

    # Local LLM (OpenAI-compatible /models) — the endpoint that was dead before
    ok, detail = await _check_http(settings.llamacpp_url.rstrip("/") + "/models")
    checks.append({"name": "llm", "ok": ok, "detail": detail})

    # Embeddings (/health on the non-/v1 root)
    emb = settings.embedding_url.rstrip("/").replace("/v1", "") + "/health"
    ok, detail = await _check_http(emb)
    checks.append({"name": "embeddings", "ok": ok, "detail": detail})

    # Extraction freshness — newest last_run_at across shells should be recent
    newest = None
    async for s in db.shell_extraction_state.find({}, {"last_run_at": 1}):
        t = s.get("last_run_at")
        if t and (newest is None or t > newest):
            newest = t
    if newest is None:
        checks.append({"name": "extraction", "ok": True, "detail": "no runs yet"})
    else:
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - newest).total_seconds() / 60
        stale_after = settings.shells_extraction_interval_minutes * 3
        checks.append({
            "name": "extraction",
            "ok": age_min <= stale_after,
            "detail": f"last run {age_min:.0f}m ago",
        })

    # Search (mongot) — Atlas text+vector search backs memory recall. It broke
    # silently once when the mongot container failed to start (bad bind mount),
    # so $search/$vectorSearch errored for days unnoticed. $listSearchIndexes
    # routes mongod -> mongot and fails fast if that gRPC channel is down.
    try:
        cur = db.memories.aggregate([{"$listSearchIndexes": {}}])
        idx = await asyncio.wait_for(cur.to_list(length=20), timeout=5.0)
        checks.append({"name": "search", "ok": True, "detail": f"mongot ok ({len(idx)} idx)"})
    except Exception as exc:
        checks.append({"name": "search", "ok": False, "detail": str(exc)[:120]})

    return checks


class SelfCheckWorker:
    """Runs run_checks() on a timer and alerts via Signal on failure/recovery."""

    def __init__(self, db, notifier, interval_minutes: int, cooldown_minutes: int):
        self.db = db
        self.notifier = notifier
        self.interval = max(60, int(interval_minutes) * 60)
        self.cooldown = max(60, int(cooldown_minutes) * 60)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._degraded = False  # for one-shot recovery notice

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="shells.selfcheck")
        logger.info("selfcheck worker started (every %ds, alert cooldown %ds)",
                    self.interval, self.cooldown)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def _alert(self, event_type: str, detail: str, cooldown: int) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.notify(
                source="selfcheck", event_type=event_type,
                detail=detail, cooldown_seconds=cooldown,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("selfcheck alert delivery failed: %s", exc)

    async def evaluate_once(self) -> list[dict]:
        """Run the checks once, fire degraded/recovered alerts, return the checks.
        Separated from the loop so it can be unit-tested."""
        checks = await run_checks(self.db)
        failed = [c for c in checks if not c["ok"]]
        if failed:
            detail = "; ".join(f"{c['name']} ({c['detail']})" for c in failed)
            logger.warning("selfcheck FAIL: %s", detail)
            self._degraded = True
            await self._alert("degraded", detail, self.cooldown)
        else:
            if self._degraded:
                self._degraded = False
                await self._alert("recovered", "all checks green again", 0)
            logger.info("selfcheck ok (%d checks)", len(checks))
        return checks

    async def _run(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=90)  # settle on boot
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.evaluate_once()
            except Exception as exc:  # pragma: no cover
                logger.warning("selfcheck tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
