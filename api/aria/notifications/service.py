"""
ARIA - Notification Service (Alerts v2)

Purpose: Cooldown-aware alerting. ProjectAria does NOT push notifications
itself — that collided with the single signal-cli daemon owned by the Hermes
agent. Instead `notify()` enqueues alerts into the `alerts` collection; the
relay pulls them over MCP (list_alerts / ack_alert) and sends them over its own
Signal. The cooldown logic is retained so a sustained outage doesn't flood the
queue.

Alerts v2 (steward proposal §3.1) adds the fields the meta layer needs to route
an alert instead of dumping every row on Ben: `severity`, `kind`, `needs_human`,
`dedup_key`/`occurrences`, `delivered_at`, `proposal`, `decision`,
`project_slug`. The relay and the triage worker select on `needs_human=true`;
everything else is cockpit + digest material. Every existing caller keeps
working unchanged — the new fields are all derived when not supplied.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from aria.config import settings
from aria.signal.service import SignalService

logger = logging.getLogger(__name__)

SEVERITIES = ("info", "low", "medium", "high", "critical")

COOLDOWN_COLLECTION = "alert_cooldowns"

# Event types that report a *return to normal* or a finished unit of work. They
# are worth recording (the digest and the cockpit read them) but nobody needs to
# be paged that something stopped being broken.
_RESOLUTION_EVENTS = frozenset(
    {"recovered", "resolved", "auto_resolved", "completed", "complete", "stopped"}
)

# Substrings that mark a genuine degradation. Deliberately conservative: an
# unmatched event type falls through to "medium", which still sets needs_human,
# so a new caller can never *lose* Ben's attention by forgetting to classify.
_HIGH_MARKERS = ("failed", "error", "exhausted", "hard_gate", "dead", "crash", "denied")


def _is_session_lifecycle(source: str) -> bool:
    """A per-session progress notice from the watchdog (`coding:<session_id>`).

    These used to be DROPPED here, which is why a stuck coding agent could never
    reach anyone: stalled:*/deadline/budget:*/loop:* all matched the drop and
    vanished. They are data now (severity=info), not silence.

    The drop existed for a real reason and that reason still has to hold: the
    Hermes triage cron spawned a fixer agent for every alert, the fixer's own
    'stopped' event became an alert, and the loop never terminated (31 rows in
    prod). Classification closes it structurally instead of by censorship —
    every consumer that *acts* on an alert (relay, triage, break-glass) selects
    `needs_human=true`, and no session-lifecycle event ever sets that flag. A
    fixer's lifecycle noise therefore cannot re-trigger the thing that spawned
    it, however many rows it writes.

    coding:gate is excluded: a C1 verification gate that gave up after N retries
    is a degradation, not a lifecycle notice.
    """
    return source.startswith("coding:") and source != "coding:gate"


def _is_mail_echo(source: str, event_type: str) -> bool:
    """Orchestrator mail the watchdog re-publishes as an alert.

    `agent_task_done` / `agent_mail` are the same closed loop as above seen from
    the mailbox side (see agents/watchdog.py _drain_orchestrator_mail); unlike
    session lifecycle they carry no cockpit value at all — the mailbox already
    holds them verbatim — so they stay dropped rather than becoming info rows.
    agent_error / agent_handoff are real problems and are NOT dropped.
    """
    return source == "task" or (
        source == "agents" and event_type in ("agent_task_done", "agent_mail")
    )


def derive_kind(source: str, event_type: str) -> str:
    """Short machine slug used for routing (break-glass allow-list, triage
    selectors, digest grouping). Caller-supplied `kind` always wins."""
    et = (event_type or "").lower()
    if source == "coding:gate" or et.startswith("gate"):
        return "gate"
    if et.startswith("stalled"):
        return "stall"
    if et.startswith("budget"):
        return "budget"
    if et.startswith("loop"):
        return "loop"
    if et == "deadline":
        return "deadline"
    base = (source or "").split(":", 1)[0].strip().lower()
    if base.startswith("coding"):
        return "session"
    slug = re.sub(r"[^a-z0-9_-]+", "-", base).strip("-")
    return slug or "alert"


def classify(source: str, event_type: str) -> tuple[str, str, bool]:
    """Return (kind, severity, needs_human) defaults for an unclassified alert.

    Default needs_human is True for anything that is not lifecycle/resolution —
    i.e. exactly the set of alerts that reach Ben today. Alerts v2 must not
    silence an existing channel as a side effect of adding a filter; narrowing
    happens deliberately, per caller, once the triage worker can justify it.
    """
    kind = derive_kind(source, event_type)
    et = (event_type or "").lower()
    if _is_session_lifecycle(source):
        return kind, "info", False
    if et in _RESOLUTION_EVENTS:
        return kind, "info", False
    if source in ("killswitch", "estop") or et in ("estop", "activated", "critical_escalation"):
        return kind, "critical", True
    if any(marker in et for marker in _HIGH_MARKERS):
        return kind, "high", True
    return kind, "medium", True


def _slug_for_path(project_path: Optional[str]) -> Optional[str]:
    """Match db.projects' slug convention (shells/harvest.py:194 uses the
    basename) so an alert can be joined to its project row."""
    if not project_path:
        return None
    return os.path.basename(project_path.rstrip("/")) or None


def _aware(value) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class NotificationService:
    """Enqueue cooldown-gated, classified alerts for the relay to deliver."""

    def __init__(self, signal_service: Optional[SignalService] = None):
        # signal_service is retained for constructor compatibility but no longer
        # used for delivery — ProjectAria queues alerts instead of sending them.
        self.signal_service = signal_service
        # Read-through cache over the `alert_cooldowns` collection. In-memory
        # alone was the bug: the dict died with the process, and 37 aria-api
        # restarts since 08-11 turned "alert once per transition" into 31
        # duplicate `selfcheck degraded` rows.
        self._cooldowns: dict[tuple[str, str], datetime] = {}

    # ------------------------------------------------------------- cooldowns

    @staticmethod
    def _cooldown_id(source: str, event_type: str) -> str:
        return f"{source}|{event_type}"

    async def _load_cooldown(self, source: str, event_type: str) -> Optional[datetime]:
        """Read a persisted cooldown, populating the in-memory cache.

        Any failure returns None (= "no record", so the alert is allowed
        through). A cooldown store that is down must never suppress an alert:
        this is a convenience path and it fails open."""
        if not getattr(settings, "alerts_cooldown_persist", True):
            return None
        try:
            from aria.db.mongodb import get_database

            db = await get_database()
            doc = await db.alert_cooldowns.find_one(
                {"_id": self._cooldown_id(source, event_type)}
            )
        except Exception as exc:
            logger.debug("cooldown load failed (%s/%s): %s", source, event_type, exc)
            return None
        last = _aware((doc or {}).get("last_sent_at"))
        if last is not None:
            self._cooldowns[(source, event_type)] = last
        return last

    async def _can_send(self, source: str, event_type: str, cooldown_seconds: int) -> bool:
        # cooldown_seconds <= 0 means "always send" — don't let same-granularity
        # repeats get suppressed by the >= comparison.
        if cooldown_seconds <= 0:
            return True
        key = (source, event_type)
        last_sent = self._cooldowns.get(key)
        if last_sent is None:
            last_sent = await self._load_cooldown(source, event_type)
        if last_sent is None:
            return True
        return datetime.now(timezone.utc) - last_sent >= timedelta(seconds=cooldown_seconds)

    async def _mark_sent(self, source: str, event_type: str) -> None:
        now = datetime.now(timezone.utc)
        self._cooldowns[(source, event_type)] = now
        if not getattr(settings, "alerts_cooldown_persist", True):
            return
        try:
            from aria.db.mongodb import get_database

            db = await get_database()
            await db.alert_cooldowns.update_one(
                {"_id": self._cooldown_id(source, event_type)},
                {
                    "$set": {
                        "last_sent_at": now,
                        "source": source,
                        "event_type": event_type,
                    }
                },
                upsert=True,
            )
        except Exception as exc:
            # In-memory already updated above, so the process still honours the
            # cooldown for its own lifetime — we only lose it across a restart,
            # which is where we started.
            logger.debug("cooldown persist failed (%s/%s): %s", source, event_type, exc)

    # ---------------------------------------------------------------- notify

    async def notify(
        self,
        *,
        source: str,
        event_type: str,
        detail: str,
        recipient: Optional[str] = None,  # accepted for compat; unused
        cooldown_seconds: int = 60,
        project_path: Optional[str] = None,
        severity: Optional[str] = None,
        kind: Optional[str] = None,
        needs_human: Optional[bool] = None,
        dedup_key: Optional[str] = None,
        project_slug: Optional[str] = None,
        proposal: Optional[dict] = None,
    ) -> dict:
        """Enqueue an alert for relay. Returns {queued: bool, ...}. Honors the
        per-(source, event_type) cooldown so repeats within the window are
        dropped (returns queued=False, reason='cooldown'). Repeats of an alert
        that is still unacked increment `occurrences` instead of inserting a
        second row (returns deduped=True)."""
        if _is_mail_echo(source, event_type):
            logger.debug("notify: dropping mail echo %s/%s (not an alert)", source, event_type)
            return {"queued": False, "reason": "informational"}

        d_kind, d_severity, d_needs_human = classify(source, event_type)
        kind = kind or d_kind
        severity = (severity or d_severity).lower()
        if severity not in SEVERITIES:
            logger.warning("notify: unknown severity %r from %s/%s", severity, source, event_type)
            severity = d_severity
        if needs_human is None:
            needs_human = d_needs_human
        needs_human = bool(needs_human)
        dedup_key = dedup_key or f"{source}|{event_type}"
        project_slug = project_slug or _slug_for_path(project_path)

        # Cooldown first, dedup second: the cooldown is the caller's own
        # statement about how often this event is worth recording at all, so a
        # suppressed repeat must not even bump `occurrences`. Callers that want
        # every recurrence counted pass cooldown_seconds=0 (the relay watchdog
        # does).
        if not await self._can_send(source, event_type, cooldown_seconds):
            return {"queued": False, "reason": "cooldown"}

        message = f"[{source}] {event_type.upper()}: {detail}"
        now = datetime.now(timezone.utc)

        try:
            from aria.db.mongodb import get_database

            db = await get_database()
        except Exception as exc:
            logger.warning("alert enqueue failed (%s/%s): %s", source, event_type, exc)
            return {"queued": False, "reason": "enqueue_failed", "detail": str(exc)}

        # Collapse onto the open row when one exists. `acked` is part of the
        # filter on purpose: once Ben has acked, a recurrence is news again and
        # deserves its own row.
        try:
            from pymongo import ReturnDocument

            existing = await db.alerts.find_one_and_update(
                {"dedup_key": dedup_key, "acked": False},
                {
                    "$inc": {"occurrences": 1},
                    "$set": {"last_seen_at": now, "detail": detail, "message": message},
                    # $max, not $set: a repeat may escalate an info row to
                    # needs_human, but a later benign repeat must never clear a
                    # raise Ben has not answered yet (false < true in BSON).
                    "$max": {"needs_human": needs_human},
                },
                sort=[("created_at", -1)],
                return_document=ReturnDocument.AFTER,
            )
        except Exception as exc:
            logger.debug("alert dedup lookup failed (%s): %s", dedup_key, exc)
            existing = None

        if existing:
            await self._mark_sent(source, event_type)
            return {
                "queued": True,
                "deduped": True,
                "alert_id": str(existing.get("_id")),
                "occurrences": int(existing.get("occurrences") or 1),
                "message": message,
            }

        doc = {
            "source": source,
            "event_type": event_type,
            "detail": detail,
            "message": message,
            "acked": False,
            "created_at": now,
            "acked_at": None,
            # Optional attribution to a project workspace (C4 cockpit filter);
            # older alerts simply lack the field.
            "project_path": project_path,
            "project_slug": project_slug,
            # --- Alerts v2 ---
            "severity": severity,
            "kind": kind,
            "needs_human": needs_human,
            "dedup_key": dedup_key,
            "occurrences": 1,
            "last_seen_at": now,
            # Set by the relay once it has actually delivered the message; the
            # RelayWatchdog reads it to tell "nothing to send" from "nobody is
            # sending".
            "delivered_at": None,
            # Written by the triage worker (diagnosis + proposed fix) and by the
            # decide route (Ben's answer). Both are owned by someone else — the
            # notify path only ever creates them empty (S3 ownership).
            "proposal": proposal,
            "decision": None,
        }
        try:
            result = await db.alerts.insert_one(doc)
        except Exception as exc:
            logger.warning("alert enqueue failed (%s/%s): %s", source, event_type, exc)
            return {"queued": False, "reason": "enqueue_failed", "detail": str(exc)}

        await self._mark_sent(source, event_type)
        return {
            "queued": True,
            "alert_id": str(result.inserted_id),
            "message": message,
            "severity": severity,
            "kind": kind,
            "needs_human": needs_human,
        }

    def status(self) -> dict:
        return {
            "persisted": bool(getattr(settings, "alerts_cooldown_persist", True)),
            "tracked_cooldowns": [
                {
                    "source": source,
                    "event_type": event_type,
                    "last_sent_at": sent_at,
                }
                for (source, event_type), sent_at in sorted(self._cooldowns.items())
            ],
        }
