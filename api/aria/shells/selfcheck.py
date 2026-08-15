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
from aria.infrastructure import gpu_devices
from aria.memory.capabilities import retrieval_capabilities

logger = logging.getLogger(__name__)


async def _check_http(
    url: str, timeout: float = 4.0, headers: dict | None = None
) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url, headers=headers or {})
        # <500 means the service is up and answering (404 on a probe path is fine).
        # 401/403 is NOT healthy though: the service answered but rejected our
        # credential, which is a real misconfiguration that would otherwise pass
        # silently — matching how /health/services grades the same statuses.
        ok = r.status_code < 500 and r.status_code not in (401, 403)
        return (ok, f"HTTP {r.status_code}")
    except Exception as exc:
        return (False, type(exc).__name__)


_GTT_ALERT_PCT = 90


def _check_gtt() -> tuple[bool, str]:
    """GPU memory pressure, per device — the actual resource that ran out and
    crashed qwen on 2026-07-28. Docker/cgroup memory limits do NOT see this:
    GPU-offloaded allocations (-ngl 999) are accounted to the DRM pool, not to
    the container's cgroup — confirmed live that day (docker stats showed ~5
    GiB combined for two containers while the GTT pool showed ~97 GiB). This
    sysfs read is the only ground-truth signal for real pressure here.

    Reads every pool rather than a fixed card. This used to be a hardcoded
    card0 read, which was correct while the box had one GPU — but the OCuLink
    R9700 made card0 the *discrete* card and card1 the Strix Halo, so the check
    started reporting the dGPU's near-empty pool and would have stayed green
    through a full Halo. `gpu_devices` classifies cards instead of trusting
    their order; a pool crossing the threshold fails the check and names
    itself, so the alert says WHICH device is under pressure.
    """
    try:
        pools = [
            pool for pool in gpu_devices.pool_snapshot()
            if pool["pool"] != gpu_devices.POOL_HOST and pool["total_gib"]
        ]
        if not pools:
            return (False, "unreadable: no GPU memory pools found")
        parts, ok = [], True
        for pool in pools:
            pct = pool["used_gib"] / pool["total_gib"] * 100
            if pct >= _GTT_ALERT_PCT:
                ok = False
            note = " SPILLING to system RAM" if pool.get("spilling") else ""
            parts.append(
                f"{pool['label']}: {pct:.0f}% "
                f"({pool['used_gib']:.0f}/{pool['total_gib']:.0f} GiB){note}"
            )
        return ok, "; ".join(parts)
    except Exception as exc:
        return (False, f"unreadable: {str(exc)[:100]}")


async def run_checks(db) -> list[dict]:
    """Return a list of {name, ok, detail} for each monitored dependency."""
    checks: list[dict] = []

    # MongoDB
    try:
        await db.command("ping")
        checks.append({"name": "mongodb", "ok": True, "detail": "ping ok"})
    except Exception as exc:
        checks.append({"name": "mongodb", "ok": False, "detail": str(exc)[:120]})

    # Local LLM (OpenAI-compatible /models) — the endpoint that was dead before.
    #
    # Since 2026-08-05 llamacpp_url is ARIA's own /llm/v1 passthrough, not a
    # fixed model port, so this check now means "is SOME local model resident
    # and serving" instead of "is one specific server up". That is the question
    # worth paging about: the previous form pointed at :8103, which DS4-0731
    # displaced (they are RAM-exclusive), so it reported ConnectError every 10
    # minutes about a server that was stopped on purpose — each one waking the
    # Hermes alert-triage cron to diagnose a non-incident. The proxy answers 503
    # when genuinely nothing is resident, which is still a real alert.
    #
    # The key is required because the proxy sits behind api_key_middleware; it
    # is harmlessly ignored if llamacpp_url is ever repointed at a raw server.
    # Timeout and retry, measured 2026-08-15: a COLD call to the passthrough
    # took 8.57 s, and the two immediately after it took 0.60 s and 0.58 s. The
    # shared 4 s default therefore turned an ordinary first-request warm-up into
    # `llm (ReadTimeout)`, which reached Ben's phone as a real alert — the fastest
    # possible way to train someone to ignore their own alert queue. One slow
    # response is not a degradation; two in a row is. Retry once before judging,
    # and give the LLM probe a budget that fits a local model rather than the
    # budget that fits a health endpoint.
    _llm_url = settings.llamacpp_url.rstrip("/") + "/models"
    _llm_headers = {"X-API-Key": settings.api_key} if settings.api_key else None
    ok, detail = await _check_http(_llm_url, timeout=15.0, headers=_llm_headers)
    if not ok:
        ok, retry_detail = await _check_http(_llm_url, timeout=15.0, headers=_llm_headers)
        detail = detail if ok else f"{detail} then {retry_detail}"
    checks.append({"name": "llm", "ok": ok, "detail": detail})

    # Chadrock (pool_api_url) — the pool-cli coding backend. Added 2026-07-28:
    # this ran completely unmonitored until then, even though it shares the
    # exact same GPU-wedge crash risk as llamacpp_url's server (both are
    # deliberately restart:"no" for that reason) and was the OTHER party in
    # that day's qwen crash. Without this, a chadrock crash pages no one.
    #
    # SKIPPED when pool_enabled is false (2026-07-30). Ben shut chadrock down
    # deliberately, so an unconditional probe reported DEGRADED every 10
    # minutes forever — and each one enqueued an alert that woke the Hermes
    # alert-triage cron, which spun up a diagnostic coding agent to
    # investigate a server that is off ON PURPOSE. A deliberately-stopped
    # service is not an incident. Mirrors how context1_enabled and the
    # /health/services probes already omit disabled backends rather than
    # counting them unhealthy.
    if settings.pool_enabled:
        ok, detail = await _check_http(settings.pool_api_url.rstrip("/") + "/models")
        checks.append({"name": "chadrock", "ok": ok, "detail": detail})

    # GPU unified-memory (GTT) pressure — see _check_gtt docstring.
    ok, detail = _check_gtt()
    checks.append({"name": "gpu_memory", "ok": ok, "detail": detail})

    # Embeddings (/health on the non-/v1 root). Skipped when the capability is
    # switched off — identical reasoning to the pool_enabled skip above: a
    # dependency that is off ON PURPOSE is not an incident, and paging about it
    # every 10 minutes is how an alert channel gets ignored. Nothing is lost by
    # skipping: memories written meanwhile carry embedding_pending and the
    # backfill worker re-embeds them the moment the capability comes back.
    if retrieval_capabilities.embeddings_enabled:
        emb = settings.embedding_url.rstrip("/").replace("/v1", "") + "/health"
        ok, detail = await _check_http(emb)
        checks.append({"name": "embeddings", "ok": ok, "detail": detail})

    # Extraction freshness — newest last_run_at across shells should be recent.
    #
    # Skipped entirely when the worker is switched off, for the same reason the
    # retrieval probes above are: a capability that is stopped ON PURPOSE must
    # never page. `SHELLS_EXTRACTION_ENABLED=false` has been set by the
    # `deepseek-research-safety.conf` drop-in since the DS4 characterization, so
    # this check reported "last run 5096m ago" on every tick — and because the
    # cooldown lived in memory and aria-api restarted 37 times, it produced 31
    # duplicate `selfcheck/degraded` alerts. That queue is what Ben stopped
    # reading. A stopped worker is not a degradation; it is a decision.
    newest = None
    if not settings.shells_extraction_enabled:
        checks.append({
            "name": "extraction",
            "ok": True,
            "detail": "disabled (shells_extraction_enabled=false) — not a degradation",
        })
    else:
        async for s in db.shell_extraction_state.find({}, {"last_run_at": 1}):
            t = s.get("last_run_at")
            if t and (newest is None or t > newest):
                newest = t
    if newest is None:
        if settings.shells_extraction_enabled:
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
    #
    # The check is skipped only when the search capability is switched OFF —
    # which is the point of the switch, and is NOT the silent-breakage case
    # above: an operator flipped it, and recall has visibly degraded to the
    # fallback scan. An unswitched mongot still gets probed exactly as before.
    if not retrieval_capabilities.search_enabled:
        return checks
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
            # Alert only on the transition INTO degraded, not every tick while it
            # stays broken — otherwise one outage = hourly Signal spam. The
            # `recovered` notice tells you when it clears; the cooldown dampens
            # flapping. (A process restart resets _degraded, so a still-broken
            # dependency re-alerts once after restart — intentional.)
            if not self._degraded:
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
