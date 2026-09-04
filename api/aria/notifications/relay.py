"""
ARIA - Relay watchdog (watching the watcher)

Purpose: notice when the outbox relay stops delivering. ARIA queues alerts and
Hermes relays them over Signal; when that relay dies, ARIA keeps queueing into a
channel nobody reads. That failure has happened silently three times
(2026-06-29→07-26, 07-28, 08-10→08-15) — each time the alerts were all present
in Mongo and none of them reached Ben.

The relay heart-beats (POST /alerts/relay-heartbeat → MCP relay_heartbeat). If
no heartbeat arrives for `alert_relay_heartbeat_timeout_minutes`, this worker:

  1. raises a `relay:dead` alert (critical, needs_human) — useless on its own,
     since the relay is exactly what would deliver it, hence:
  2. writes vault/ProjectAria/Planning/STEWARD_INBOX.md, which LiveSync lands on
     Ben's phone without any ARIA→Ben push at all, and
  3. sends ONE break-glass Signal message via the signal-cli JSON-RPC daemon
     (steward proposal §6.4 / decision D5 — the single sanctioned exception to
     "ARIA never pushes"), rate-limited to one per timeout window.

Deliberately conservative: a heartbeat must have been recorded at least once
before death can be declared, so a fresh install (or a box where the relay cron
was never installed) never pages.

Related: proposal §3.1 item 3, §6.3, §6.4.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from aria.config import settings
from aria.notifications import signal_rpc

logger = logging.getLogger(__name__)

RELAY_STATE_ID = "relay_state"

# The alert this worker raises, and the key the break-glass allow-list is
# written in (settings.alert_breakglass_kinds defaults to ["relay:dead",
# "estop"]). Alert rows carry kind="relay"; the allow-list is checked with the
# fully-qualified key so listing relay:dead cannot authorise relay:recovered.
RELAY_KIND = "relay"
BREAKGLASS_KEY = "relay:dead"

INBOX_RELATIVE_PATH = ("ProjectAria", "Planning", "STEWARD_INBOX.md")

_INBOX_MAX_ALERTS = 40


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def record_heartbeat(db, source: str = "relay") -> dict:
    """Upsert the relay liveness marker; returns the state BEFORE this beat.

    Module-level so the route can record a heartbeat even when the worker is
    disabled or absent — losing the heartbeat because the watchdog is off would
    make enabling the watchdog later look like an instant outage."""
    now = _utcnow()
    previous = await db.app_state.find_one({"_id": RELAY_STATE_ID}) or {}
    await db.app_state.update_one(
        {"_id": RELAY_STATE_ID},
        {
            "$set": {
                "last_heartbeat_at": now,
                "source": source,
                "updated_at": now,
                # Cleared here rather than in the worker: the heartbeat IS the
                # recovery event, and the route path must clear it too or a
                # watchdog-less deployment stays "dead" forever.
                "dead_since": None,
            },
            "$inc": {"heartbeat_count": 1},
        },
        upsert=True,
    )
    return dict(previous)


class RelayWatchdog:
    """Timer worker: relay liveness → alert + vault inbox + break-glass Signal."""

    def __init__(
        self,
        db,
        notifier=None,
        *,
        interval_seconds: Optional[int] = None,
        timeout_minutes: Optional[int] = None,
        obsidian_writer=None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.db = db
        self.notifier = notifier
        self.interval = max(10, int(interval_seconds or settings.alert_relay_watchdog_interval_seconds))
        self.timeout = timedelta(
            minutes=max(1, int(timeout_minutes or settings.alert_relay_heartbeat_timeout_minutes))
        )
        self._writer = obsidian_writer
        # Injected clock so the death/rate-limit windows are testable without
        # sleeping through a 20-minute timeout.
        self._now = now or _utcnow
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last: dict = {"checked_at": None, "reason": "not_run"}

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        if not settings.alert_relay_watchdog_enabled:
            logger.info("relay watchdog disabled by settings")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="notifications.relay_watchdog")
        logger.info(
            "relay watchdog started (every %ds, timeout %dm)",
            self.interval,
            int(self.timeout.total_seconds() // 60),
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        # Settle on boot: the relay cron runs on its own schedule, and paging
        # during the first minute of an aria-api restart would just re-raise the
        # outage the restart may have fixed.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=min(90, self.interval))
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.evaluate_once()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("relay watchdog tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    # ---------------------------------------------------------------- state

    def status(self) -> dict:
        """Last evaluated state. Synchronous (matches the other workers) — it
        reports the most recent tick, not a fresh DB read."""
        return dict(self._last)

    async def record_heartbeat(self, source: str = "relay") -> dict:
        """Record a relay heartbeat; raises a recovery alert if it ends an
        outage this worker had declared."""
        previous = await record_heartbeat(self.db, source)
        now = self._now()
        recovered = bool(previous.get("dead_since"))
        if recovered:
            down_for = now - (_aware(previous.get("dead_since")) or now)
            await self._notify(
                event_type="recovered",
                detail=(
                    f"Signal relay is delivering again after "
                    f"{down_for.total_seconds() / 60:.0f}m of silence"
                ),
                severity="info",
                needs_human=False,
                dedup_key="relay|recovered",
            )
            # The dead-state inbox is a generated status surface. Leaving its
            # warning in place after the heartbeat recovers makes a healthy
            # relay look broken indefinitely, so refresh it in the same
            # recovery transaction with the still-pending human alerts.
            pending = await self._pending_alerts()
            await self.write_inbox(pending)
        self._last = {
            "checked_at": now,
            "reason": "heartbeat",
            "dead": False,
            "recovered": recovered,
            "last_heartbeat_at": now,
            "source": source,
        }
        return self._last

    async def evaluate_once(self) -> dict:
        """One liveness check. Returns a small dict describing what it decided.
        Separated from the loop so it can be unit-tested with a fake clock."""
        now = self._now()
        try:
            state = await self.db.app_state.find_one({"_id": RELAY_STATE_ID}) or {}
        except Exception as exc:
            logger.warning("relay watchdog: state read failed: %s", exc)
            self._last = {"checked_at": now, "reason": "state_unavailable", "dead": False}
            return self._last

        last_beat = _aware(state.get("last_heartbeat_at"))
        pending = await self._pending_alerts()
        if last_beat is None:
            # A relay that has NEVER beaten is indistinguishable from a relay
            # that was never installed. Paging on a fresh checkout would train
            # exactly the ignore-reflex this whole subsystem exists to prevent.
            self._last = {
                "checked_at": now,
                "reason": "no_heartbeat_yet",
                "dead": False,
                "pending": len(pending),
            }
            return self._last

        age = now - last_beat
        if age <= self.timeout:
            self._last = {
                "checked_at": now,
                "reason": "alive",
                "dead": False,
                "last_heartbeat_at": last_beat,
                "age_minutes": age.total_seconds() / 60,
                "pending": len(pending),
            }
            return self._last

        return await self._declare_dead(state, now, last_beat, age, pending)

    # ----------------------------------------------------------------- death

    async def _declare_dead(
        self, state: dict, now: datetime, last_beat: datetime, age: timedelta, pending: list[dict]
    ) -> dict:
        age_minutes = age.total_seconds() / 60
        dead_since = _aware(state.get("dead_since")) or now
        try:
            await self.db.app_state.update_one(
                {"_id": RELAY_STATE_ID},
                {"$set": {"dead_since": dead_since, "updated_at": now}},
                upsert=True,
            )
        except Exception as exc:
            logger.warning("relay watchdog: could not persist dead_since: %s", exc)

        detail = (
            f"No Signal relay heartbeat for {age_minutes:.0f}m "
            f"(last {last_beat.isoformat()}); {len(pending)} alert(s) awaiting a human"
        )
        # cooldown 0 + a stable dedup_key: while the relay stays dead each tick
        # increments occurrences on the one open row instead of writing a new
        # one, so the queue Ben eventually reads has one line, not 300.
        alert = await self._notify(
            event_type="dead",
            detail=detail,
            severity="critical",
            needs_human=True,
            dedup_key="relay|dead",
        )

        inbox_path = await self.write_inbox(pending, dead_detail=detail)
        breakglass = await self._maybe_breakglass(state, now, detail)

        self._last = {
            "checked_at": now,
            "reason": "dead",
            "dead": True,
            "dead_since": dead_since,
            "last_heartbeat_at": last_beat,
            "age_minutes": age_minutes,
            "pending": len(pending),
            "alert": alert,
            "inbox_path": inbox_path,
            "breakglass": breakglass,
        }
        return self._last

    async def _maybe_breakglass(self, state: dict, now: datetime, detail: str) -> dict:
        last_sent = _aware(state.get("last_breakglass_at"))
        if last_sent is not None and now - last_sent < self.timeout:
            return {"sent": False, "reason": "rate_limited"}
        result = await signal_rpc.send_breakglass(
            f"[ARIA break-glass] {detail}. "
            f"Alerts are queued but undelivered — see STEWARD_INBOX.md in the vault.",
            kind=BREAKGLASS_KEY,
        )
        if result.get("sent"):
            # Persisted, not in-memory: an aria-api restart loop would otherwise
            # send one message per restart.
            try:
                await self.db.app_state.update_one(
                    {"_id": RELAY_STATE_ID},
                    {"$set": {"last_breakglass_at": now}},
                    upsert=True,
                )
            except Exception as exc:
                logger.warning("relay watchdog: could not persist break-glass stamp: %s", exc)
        return result

    # ----------------------------------------------------------------- inbox

    async def _pending_alerts(self) -> list[dict]:
        try:
            cursor = (
                self.db.alerts.find({"needs_human": True, "acked": False})
                .sort("created_at", -1)
                .limit(_INBOX_MAX_ALERTS)
            )
            return await cursor.to_list(length=_INBOX_MAX_ALERTS)
        except Exception as exc:
            logger.warning("relay watchdog: pending-alert read failed: %s", exc)
            return []

    async def refresh_inbox(self) -> Optional[str]:
        """Refresh the generated inbox after the alert queue changes.

        Alert decisions and acknowledgements happen through API routes, not the
        watchdog timer.  Without an explicit refresh here, Mongo is correct but
        the vault can continue advertising already-closed alerts indefinitely.
        Preserve the relay-down warning when the persisted liveness state still
        describes a real outage.
        """
        pending = await self._pending_alerts()
        dead_detail = ""
        try:
            state = await self.db.app_state.find_one({"_id": RELAY_STATE_ID}) or {}
            last_beat = _aware(state.get("last_heartbeat_at"))
            if _aware(state.get("dead_since")) is not None and last_beat is not None:
                age = self._now() - last_beat
                if age > self.timeout:
                    dead_detail = (
                        f"No Signal relay heartbeat for {age.total_seconds() / 60:.0f}m "
                        f"(last {last_beat.isoformat()}); "
                        f"{len(pending)} alert(s) awaiting a human"
                    )
        except Exception as exc:
            # The alert mutation has already succeeded.  A fallback-status
            # refresh must never turn that success into an API failure.
            logger.warning("relay watchdog: liveness read during inbox refresh failed: %s", exc)
        return await self.write_inbox(pending, dead_detail=dead_detail)

    def inbox_path(self) -> Path:
        return Path(settings.obsidian_vault_path).joinpath(*INBOX_RELATIVE_PATH)

    async def write_inbox(self, pending: list[dict], *, dead_detail: str = "") -> Optional[str]:
        """Write/refresh STEWARD_INBOX.md — the delivery path that needs no
        relay at all (LiveSync carries it to the phone). Returns the path, or
        None if the vault is unavailable. Never raises: this is a fallback, and
        a fallback that can fail the caller is not one.

        Deliberately NOT gated on `obsidian_enabled`: that switch governs ARIA
        publishing long-form documents, and a publishing preference must not be
        able to silence the break-glass channel. The file is ARIA-owned and
        rewritten whole each time — Ben answers through Signal or
        POST /alerts/{id}/decide, never by editing it."""
        path = self.inbox_path()
        content = self._render_inbox(pending, dead_detail=dead_detail)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(path, content)

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            logger.warning("relay watchdog: inbox write failed (%s): %s", path, exc)
            return None
        logger.info("relay watchdog: refreshed %s (%d pending)", path, len(pending))
        return str(path)

    def _atomic_write(self, path: Path, content: str) -> None:
        """Reuse ObsidianWriter's temp-file+rename so the LiveSync bridge never
        replicates a half-written note. Falls back to the same pattern inline if
        that private helper ever moves — a break-glass path must not depend on
        another module's refactor."""
        writer = self._writer
        if writer is None:
            try:
                from aria.integrations.obsidian import ObsidianWriter

                writer = ObsidianWriter(settings.obsidian_vault_path)
            except Exception:  # pragma: no cover - import guard
                writer = None
        atomic = getattr(writer, "_atomic_write", None)
        if callable(atomic):
            atomic(path, content)
            return
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def _render_inbox(self, pending: list[dict], *, dead_detail: str = "") -> str:
        now_local = self._now().astimezone()
        stamp = now_local.isoformat(timespec="seconds")
        lines = [
            "---",
            "owner: ARIA (generated — edits are overwritten)",
            f"updated: {stamp}",
            "tags:",
            "  - aria",
            "  - steward",
            "  - inbox",
            "---",
            "",
            "# ARIA steward inbox",
            "",
            f"Last updated: {stamp}",
            "",
        ]
        if dead_detail:
            lines += [
                "> [!warning] Signal relay is not delivering",
                f"> {dead_detail}",
                ">",
                "> This file is the fallback channel: it reaches you through LiveSync,",
                "> not through the relay. Restart the Hermes outbox cron to resume",
                "> normal delivery.",
                "",
            ]
        if not pending:
            lines += ["No alerts are waiting on a human.", ""]
        else:
            lines += [f"## {len(pending)} alert(s) waiting on you", ""]
            for doc in pending:
                created = _aware(doc.get("created_at"))
                when = created.astimezone().strftime("%Y-%m-%d %H:%M") if created else "unknown"
                occurrences = int(doc.get("occurrences") or 1)
                repeat = f" ×{occurrences}" if occurrences > 1 else ""
                lines.append(
                    f"- **{str(doc.get('severity') or 'medium').upper()}** "
                    f"`{doc.get('kind') or 'alert'}` [{doc.get('source')}] "
                    f"{doc.get('event_type')}{repeat} — {when}"
                )
                detail = str(doc.get("detail") or "").strip().replace("\n", " ")
                if detail:
                    lines.append(f"  - {detail[:400]}")
                proposal = doc.get("proposal") or {}
                if isinstance(proposal, dict) and proposal.get("summary"):
                    lines.append(f"  - proposed fix: {str(proposal['summary'])[:400]}")
                lines.append(f"  - id: `{doc.get('_id')}`")
            lines += [
                "",
                "Reply through Hermes with `APPLY <id>` / `REJECT <id>` / `STOP <id>` /",
                "`HOLD <id>` / `IGNORE <id>`, or POST /api/v1/alerts/<id>/decide.",
                "",
            ]
        return "\n".join(lines)

    # ---------------------------------------------------------------- alerts

    async def _notify(
        self,
        *,
        event_type: str,
        detail: str,
        severity: str,
        needs_human: bool,
        dedup_key: str,
    ) -> dict:
        if self.notifier is None:
            return {"queued": False, "reason": "no_notifier"}
        try:
            return await self.notifier.notify(
                source=RELAY_KIND,
                event_type=event_type,
                detail=detail,
                cooldown_seconds=0,
                severity=severity,
                kind=RELAY_KIND,
                needs_human=needs_human,
                dedup_key=dedup_key,
            )
        except Exception as exc:
            logger.warning("relay watchdog: alert enqueue failed: %s", exc)
            return {"queued": False, "reason": "enqueue_failed", "detail": str(exc)[:200]}
