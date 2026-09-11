"""Detect a model backend that claims to be working but is not.

The failure this exists for, observed 2026-09-11 09:16-09:38 on the CUDA/Halo
candidate: `llama-server` reported `is_processing=true` on its only slot while
the main thread spun in userspace (state R, wchan 0), 105 worker threads sat on
futexes, and the dGPU held 1695 MHz at 0% utilisation. Nothing was computing.
A request sent straight to the backend, bypassing ARIA entirely, timed out
having received zero bytes.

Two properties made that outage silent for ~22 minutes:

  * The gateway's read timeout is 1800s (llm_proxy._TIMEOUT), chosen so a slow
    long-context generation is never cut off. A wedged backend therefore holds
    the only admission slot for half an hour.
  * Everything queues behind that slot, so callers see latency, not an error.
    `aria-deploy-mac`'s idle gate noticed before any human did.

Upstream has no fix: llama.cpp issue 23268 reports the same shape (Strix Halo,
Vulkan, A3B MoE, --spec-type draft-mtp) and is open and unconfirmed, so the
right posture is detection and recovery rather than waiting for a patch.

What counts as stalled, deliberately narrowly:

    requests_processing > 0            the backend says it is busy, AND
    tokens_predicted_total unchanged   no token has been produced, AND
    for at least `stall_after_seconds` across consecutive polls

A generation that is merely slow still increments `tokens_predicted_total`, and
a long prefill produces no tokens but also reports no progress, so the floor is
set well above the slowest observed prefill (233k tokens took 13m39s on
2026-09-11, ~295 tok/s) rather than at a decode-shaped threshold. Prefer a late
alert to a false one that trains people to ignore it.

This module only OBSERVES and RAISES. It does not restart the backend: clearing
the wedge means bouncing a model server that Hermes and Pi depend on, and that
decision stays with the operator.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# 233k-token prefill measured at 13m39s on 2026-09-11. A stall must outlast the
# slowest legitimate silence by a clear margin, so this is not a decode-latency
# threshold and should not be tuned down to one.
DEFAULT_STALL_AFTER_SECONDS = 1200.0
DEFAULT_INTERVAL_SECONDS = 60.0


@dataclass
class _Progress:
    """Last point at which this backend demonstrably did work."""

    tokens: Optional[int]
    since: float
    alerted: bool = False


class BackendStallWatcher:
    """Poll runtime stats; raise when a backend claims work but produces none.

    Start/stop/_run pattern, as ScanReconcileWorker (aria/shared/scan.py).
    """

    def __init__(
        self,
        probe: Callable[[], Awaitable[dict[str, Any]]],
        alert: Callable[..., Awaitable[Any]],
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        stall_after_seconds: float = DEFAULT_STALL_AFTER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._probe = probe
        self._alert = alert
        self.interval = max(5.0, float(interval_seconds))
        self.stall_after = max(60.0, float(stall_after_seconds))
        self._clock = clock
        self._seen: dict[str, _Progress] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="infrastructure.stall_watch")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                # A watcher that dies on a bad poll is worse than no watcher:
                # it would look healthy while watching nothing.
                logger.warning("stall-watch: tick failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> list[dict[str, Any]]:
        """One poll. Returns the backends newly judged stalled this tick."""
        stats = await self._probe()
        now = self._clock()
        stalled: list[dict[str, Any]] = []

        for slug, row in (stats or {}).items():
            processing = _as_int(row.get("requests_processing"))
            tokens = _as_int(row.get("tokens_predicted_total"))

            # Not claiming to be busy, or the probe could not say: forget any
            # history. An unreachable backend is a different alert, not this one.
            if not processing:
                self._seen.pop(slug, None)
                continue

            prior = self._seen.get(slug)
            if prior is None or tokens is None or prior.tokens != tokens:
                # Progress, or the first time we have seen it busy. Either way
                # this is the new reference point.
                self._seen[slug] = _Progress(tokens=tokens, since=now)
                continue

            stuck_for = now - prior.since
            if stuck_for < self.stall_after or prior.alerted:
                continue

            prior.alerted = True
            detail = (
                f"{slug} reports requests_processing={processing} but "
                f"tokens_predicted_total has been frozen at {tokens} for "
                f"{int(stuck_for)}s. Nothing is being generated and every "
                f"caller is queued behind the slot. Known upstream shape: "
                f"llama.cpp issue 23268 (open). Clearing it means restarting "
                f"the model server, which is an operator decision."
            )
            logger.error("stall-watch: %s", detail)
            stalled.append({"slug": slug, "detail": detail,
                            "stalled_seconds": int(stuck_for), "tokens": tokens})
            try:
                await self._alert(
                    source="inference",
                    event_type="backend_stalled",
                    detail=detail,
                    severity="critical",
                    needs_human=True,
                    dedup_key=f"backend_stalled:{slug}",
                )
            except Exception:  # noqa: BLE001
                # Fail open, as gitguard._alert does: the log line above is the
                # durable half and must not be lost to a down alert backend.
                logger.warning("stall-watch: could not raise alert for %s", slug,
                               exc_info=True)

        return stalled


def _as_int(value: Any) -> Optional[int]:
    """Prometheus counters arrive as floats; absent ones as None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def probe_running_backends(manager, db) -> Callable[[], Awaitable[dict[str, Any]]]:
    """Default probe: the same runtime stats `/model-servers/utilization` serves.

    Deliberately reuses `probe_runtime` rather than opening a second telemetry
    path, so the watcher can never disagree with what the cockpit shows.
    Anything that fails to probe is omitted, not reported as idle — a null has
    always meant UNKNOWN here.
    """

    async def probe() -> dict[str, Any]:
        from aria.infrastructure.llm_route import is_servable
        from aria.infrastructure.model_servers import _BY_SLUG, probe_runtime

        servers = await manager.status(db)
        running = [s for s in servers if is_servable(s)]
        out: dict[str, Any] = {}
        for server in running:
            spec = _BY_SLUG.get(server["slug"])
            if spec is None:
                continue
            try:
                stats = await probe_runtime(spec)
            except Exception:  # noqa: BLE001
                continue
            if stats is None:
                continue
            out[server["slug"]] = {
                "requests_processing": stats.requests_processing,
                "tokens_predicted_total": stats.tokens_predicted_total,
            }
        return out

    return probe
