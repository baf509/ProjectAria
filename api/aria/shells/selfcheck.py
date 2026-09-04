"""Self-monitoring + alerting for the aria-shells stack.

Periodically verifies the things that silently broke before (a dead model
endpoint, stalled extraction, an unreachable DB) and pushes a Signal alert via
the existing NotificationService when something is wrong — with a cooldown so a
sustained outage doesn't spam. A recovery notice is sent once when it clears.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

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


# NOTE: there is deliberately no "pool is N% full" threshold any more.
#
# It fired permanently. `Qwen3.8-27B-R9700-Radiance` occupies 29 of the R9700's
# 31.9 GiB — the card's entire purpose — so the pool sits at ~93% for as long as
# the intended configuration is running, and the check paged every time with
# nothing for a human to do. An alert that fires on the designed state is how a
# person is trained to stop reading the queue (the same argument the alerts-v2
# needs_human lane is built on).
#
# A model sized to fill its card is FIT, not pressure. What is actually
# actionable is a dGPU model that no longer fits and starts consuming system RAM
# — `spilling`, the one documented coupling between the two pools. Raw headroom
# is enforced where it belongs: the start-time gate refuses a launch that will
# not fit, and that refusal arrives when you are trying to do something about
# it.


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
    if sys.platform == "darwin":
        return (
            True,
            "not applicable on the Mac control plane; inference GPU pools are node-observed",
        )

    try:
        pools = [
            pool for pool in gpu_devices.pool_snapshot()
            if pool["pool"] != gpu_devices.POOL_HOST and pool["total_gib"]
        ]
        if not pools:
            return (False, "unreadable: no GPU memory pools found")

        parts, problems = [], []
        for pool in pools:
            pct = pool["used_gib"] / pool["total_gib"] * 100
            spilling = bool(pool.get("spilling"))
            if spilling:
                problems.append(
                    f"{pool['label']} is SPILLING into system RAM — it no longer "
                    "fits its own card, so it is now competing with the Halo's pool"
                )
            parts.append(
                f"{pool['label']}: {pct:.0f}% "
                f"({pool['used_gib']:.0f}/{pool['total_gib']:.0f} GiB)"
                f"{' SPILLING' if spilling else ''}"
            )

        detail = "; ".join(parts)
        if problems:
            return (False, "; ".join(problems) + f" [{detail}]")
        return (True, detail)
    except Exception as exc:
        return (False, f"unreadable: {str(exc)[:100]}")


VAULT_UNREADABLE_SAMPLE = 5


def _check_vault_readable(vault_path: str) -> dict:
    """Every vault file must be readable by the process running the bridge.

    Synchronous and run in a thread: this is a filesystem walk, and the event
    loop serves the health endpoint.

    This used to test the world-readable bit (``S_IROTH``). That was a proxy for
    "a *different* user can read this", correct only while the bridge ran as a
    dedicated service account against files owned by someone else. Since the
    2026-08-29 account unification the bridge runs as the owner, so 0600 files
    are perfectly readable and the bit test reported a permanent false failure.
    Test real access instead of guessing from a permission bit.
    """
    root = Path(vault_path)
    if not root.is_dir():
        return {"name": "vault", "ok": True, "detail": f"no vault at {vault_path} (skipped)"}
    offenders: list[str] = []
    scanned = 0
    try:
        for path in root.rglob("*"):
            # `.git` is the vault's own backup repo; the bridge is configured to
            # skip it and its object files are legitimately mode-varied.
            if ".git" in path.parts or ".trash" in path.parts:
                continue
            if not path.is_file():
                continue
            scanned += 1
            if not os.access(path, os.R_OK):
                offenders.append(str(path.relative_to(root)))
                if len(offenders) >= VAULT_UNREADABLE_SAMPLE:
                    break
    except OSError as exc:
        return {"name": "vault", "ok": False, "detail": f"walk failed: {str(exc)[:100]}"}
    if offenders:
        return {
            "name": "vault",
            "ok": False,
            "detail": (
                f"{len(offenders)}+ file(s) unreadable by the livesync bridge "
                f"— sync stops for the WHOLE vault: "
                + ", ".join(offenders)
            ),
        }
    return {"name": "vault", "ok": True, "detail": f"{scanned} files readable"}


async def run_checks(db) -> list[dict]:
    """Return a list of {name, ok, detail} for each monitored dependency."""
    checks: list[dict] = []

    # MongoDB
    try:
        await db.command("ping")
        checks.append({"name": "mongodb", "ok": True, "detail": "ping ok"})
    except Exception as exc:
        checks.append({"name": "mongodb", "ok": False, "detail": str(exc)[:120]})

    # Fleet registry invariants. Connectivity and semantic activity are
    # independent, but several combinations are impossible and indicate drift
    # rather than a real shell state.
    try:
        now = datetime.now(timezone.utc)
        nodes: dict[str, dict] = {}
        async for node in db.nodes.find({}, {"last_heartbeat_at": 1}):
            nodes[str(node.get("_id"))] = node
        problems: list[str] = []
        async for shell in db.shells.find(
            {"status": {"$in": ["active", "idle"]}},
            {"name": 1, "host": 1, "last_seen_at": 1},
        ):
            host = str(shell.get("host") or "")
            if not host or host == settings.local_node_id:
                continue
            node = nodes.get(host)
            if node is None:
                problems.append(f"{shell.get('name')}: unregistered host {host}")
                continue
            heartbeat = node.get("last_heartbeat_at")
            if heartbeat and heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            online = bool(heartbeat) and (
                now - heartbeat
            ).total_seconds() < settings.node_heartbeat_timeout_seconds
            if online and not shell.get("last_seen_at"):
                problems.append(f"{shell.get('name')}: online remote shell has no last_seen_at")
        checks.append(
            {
                "name": "fleet_registry",
                "ok": not problems,
                "detail": "consistent" if not problems else "; ".join(problems[:5]),
            }
        )
    except Exception as exc:
        checks.append({"name": "fleet_registry", "ok": False, "detail": str(exc)[:120]})

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

    # Vault readability — the sync path carries Ben's APPROVALS now.
    #
    # The obsidian-livesync bridge reads this vault from a container as a
    # different uid. One file it cannot read does not degrade its sync: it
    # kills the `corsair-files` peer at startup with EACCES and stops
    # disk->phone sync for the WHOLE vault. That happened on 2026-08-17 and ran
    # undetected for two days, because the container stayed up the entire time
    # and every container-level check therefore said "healthy".
    #
    # This checks the CAUSE rather than the symptom, so it fires before the
    # bridge next restarts onto the landmine rather than after: an unreadable
    # file is a fault whether or not the peer has tripped on it yet. Cheap
    # enough to run every tick — a vault is thousands of small files, and the
    # walk stops at the first handful of offenders.
    if settings.obsidian_enabled:
        checks.append(await asyncio.get_running_loop().run_in_executor(
            None, _check_vault_readable, settings.obsidian_vault_path
        ))

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
