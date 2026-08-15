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

Two of those fields carry the load-bearing distinctions and are easy to blur:
`needs_human` is the only thing a robot acts on (relay, triage, break-glass), so
nothing that a fixer agent can itself emit may ever set it; `severity` is what a
human read model ranks on, escalates on repeats, and — at `info` — is what the
`expires_at` TTL is allowed to reap.
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
# Ordered, so "worse than" is a comparison and not a string sort. BSON `$max`
# on the severity string would order it alphabetically — "critical" < "info" <
# "low" < "medium" — i.e. the worst level we have loses to three of the four
# others. Anything that raises a severity goes through severity_rank().
_SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITIES)}

COOLDOWN_COLLECTION = "alert_cooldowns"

# Info rows are the record, not the ask: session stopped/completed/stall/
# budget/loop all land here and nobody ever acks one. Unbounded, they made
# every coding session permanently inflate the cockpit's attention score and
# eventually filled its 300-row read cap with lifecycle noise that hid the real
# alerts. `expires_at` + the TTL index in main.py gives them a bounded life;
# everything that needs a human carries expires_at=None and is never reaped.
_INFO_ALERT_TTL_DAYS = 7

# A persisted cooldown is one document on a mongod bound 0.0.0.0 with no auth
# (CLAUDE.md S4), writable by anything on the tailnet including a coding agent,
# and since Alerts v2 it survives restarts. A `last_sent_at` in the future
# silences its (source, event_type) *forever* — `now - last_sent >= cooldown`
# can never become true again. These bound what we are willing to believe.
_COOLDOWN_CLOCK_SKEW = timedelta(minutes=5)
_COOLDOWN_MAX_AGE = timedelta(days=30)

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

# Terminal outcomes of a coding session that are NOT "it finished". session.py
# emits `error` for a non-zero exit; watchdog.py emits `budget:hard_gate` when
# the budget guard checkpoints and stops a session, and `deadline` when it runs
# out of time. All three ended in the same silent bucket as "completed" — which
# made the headline claim of Alerts v2 (a stuck or dead agent becomes visible)
# false for the cases that matter most. They are *severity* now, never
# needs_human; see _is_session_failure for why that distinction is the whole
# anti-loop argument.
_SESSION_FAILURE_EVENTS = frozenset(
    {"error", "failed", "failure", "crashed", "killed", "aborted", "deadline"}
)


def severity_rank(value) -> int:
    """Position in SEVERITIES, or -1 for missing/unknown (a pre-v2 row has no
    severity at all, and anything real must be able to raise it)."""
    return _SEVERITY_RANK.get(str(value or "").lower(), -1)


def _expiry(severity: str, now: datetime) -> Optional[datetime]:
    """When this row should self-delete, or None for "never" — the TTL index
    ignores documents whose field is null or missing, so None is the durable
    case rather than a special one."""
    if severity != "info":
        return None
    try:
        days = float(getattr(settings, "alerts_info_ttl_days", _INFO_ALERT_TTL_DAYS))
    except (TypeError, ValueError):
        days = _INFO_ALERT_TTL_DAYS
    return now + timedelta(days=days) if days > 0 else None


def _is_session_lifecycle(source: str) -> bool:
    """A per-session progress notice from the watchdog (`coding:<session_id>`).

    These used to be DROPPED here, which is why a stuck coding agent could never
    reach anyone: stalled:*/deadline/budget:*/loop:* all matched the drop and
    vanished. They are data now, not silence — info for progress notices, and a
    real severity for the ones that report a broken unit of work
    (_is_session_failure).

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


def _is_session_failure(event_type: str) -> bool:
    """A session-lifecycle event that reports a BROKEN unit of work.

    "session completed" and "session failed" are not the same fact, and until
    now both were `info, needs_human=False` — so a session that exited non-zero,
    was killed by the budget guard, or ran out its deadline reached exactly
    nobody. These get a real severity (the cockpit's attention query and the
    digest both rank on severity), while `needs_human` stays False.

    That split is what keeps the loop closed. The 31-row incident was: triage
    cron selects an alert → spawns a fixer agent → the fixer's own session
    lifecycle becomes an alert → triage selects it → repeat. Every consumer that
    *acts* on an alert (relay, triage, break-glass) selects `needs_human=true`,
    and no `coding:<session_id>` event of any kind — failure included — ever
    sets it. So a fixer agent that fails, blows its budget and times out writes
    three high-severity rows and re-triggers nothing: the only consumers that
    see them are read models a human looks at. Severity moves what a human
    *sees*; needs_human moves what a robot *does*; only the second can loop.
    """
    et = (event_type or "").lower()
    if et in _RESOLUTION_EVENTS:
        return False
    return et in _SESSION_FAILURE_EVENTS or et.startswith("budget:hard_gate")


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
        if _is_session_failure(et):
            return kind, "high", False
        return kind, "info", False
    if et in _RESOLUTION_EVENTS:
        return kind, "info", False
    if source in ("killswitch", "estop") or et in ("estop", "activated", "critical_escalation"):
        return kind, "critical", True
    if any(marker in et for marker in _HIGH_MARKERS):
        return kind, "high", True
    return kind, "medium", True


def _slug_for_path(project_path: Optional[str]) -> Optional[str]:
    """Last-resort slug for a path no db.projects row claims. Harvest mints new
    rows from the directory basename, so this is the right guess for a workspace
    that has never been harvested — and the wrong one for everything else, which
    is why it is the fallback and not the answer (see resolve_project_slug)."""
    if not project_path:
        return None
    return os.path.basename(project_path.rstrip("/")) or None


def _ancestor_paths(project_path: str) -> list[str]:
    """The path and every parent directory — the only roots that could own it.
    Turns "which project claims this path?" into one indexed `$in` instead of
    loading all ~50 project rows on every alert."""
    out: list[str] = []
    current = project_path.rstrip("/")
    while current and current != "/":
        out.append(current)
        current = os.path.dirname(current)
    return out


async def resolve_project_slug(db, project_path: Optional[str]) -> Optional[str]:
    """Resolve a workspace path to the slug of the db.projects row that owns it.

    The basename is NOT the slug. `/home/ben/Development/ProjectAria` is slug
    **`aria`** — a hand-created row claiming the path through relevant_paths
    (shells/harvest.py documents exactly this, and merges into it rather than
    minting a `ProjectAria` twin) — and every ambient-extractor project is
    lowercased and hyphenated by planning/service.py:_slugify. Writing the
    basename meant the cockpit's `GET /alerts?project=aria` matched nothing at
    all: not `project_slug`, and not the route's `/{project}/?$` path regex
    either. The alerts were in Mongo and the project view was empty.

    Ownership is most-specific-root-wins (the cockpit's PathIndex, reused rather
    than re-derived) — a coarse harvested row for ~/Development must not claim
    ProjectAria's alerts, the same failure PathIndex exists to prevent for
    shells, sessions and memories.

    Falls back to the basename when nothing claims the path (a brand-new
    workspace, or a DB that is unreachable): a slightly wrong slug is worth more
    than no attribution at all, and harvest will mint that same slug later.
    """
    if not project_path:
        return None
    fallback = _slug_for_path(project_path)
    candidates = _ancestor_paths(project_path)
    if not candidates:
        return fallback
    try:
        docs = await db.projects.find(
            {"$or": [{"path": {"$in": candidates}}, {"relevant_paths": {"$in": candidates}}]},
            {"slug": 1, "path": 1, "relevant_paths": 1},
        ).to_list(length=50)
    except Exception as exc:
        logger.debug("project slug lookup failed for %s: %s", project_path, exc)
        return fallback
    if not docs:
        return fallback
    try:
        # Imported here, not at module scope: aria.api.deps imports this service,
        # so a top-level import of a routes module closes the cycle.
        from aria.api.routes.digest import PathIndex

        return PathIndex.from_docs(docs).owner(project_path) or fallback
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("project slug attribution failed for %s: %s", project_path, exc)
        return fallback


def _aware(value) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _validated_last_sent(value, *, source: str, event_type: str, now: datetime) -> Optional[datetime]:
    """A stored cooldown stamp we are willing to act on, else None (= no record,
    so the alert goes through).

    The old code fed whatever was in the document straight into the arithmetic.
    A stamp in the future makes `now - last_sent >= cooldown` false forever, so
    one write — reachable by anything holding the global API key, on an
    unauthenticated mongod — permanently silences an alert class, and now
    survives restarts too. Garbage stamps are loud (WARNING, not debug): a
    suppressed alert class is precisely the failure this subsystem exists to
    notice, and the next `_mark_sent` overwrites the poisoned value anyway, so
    the log line is the only evidence it ever happened.
    """
    last = _aware(value)
    if last is None:
        if value is not None:
            logger.warning(
                "cooldown %s/%s has a non-datetime last_sent_at (%r) — ignoring it",
                source, event_type, value,
            )
        return None
    if last > now + _COOLDOWN_CLOCK_SKEW:
        logger.warning(
            "cooldown %s/%s is stamped %s in the FUTURE (%s) — ignoring it; a future "
            "stamp suppresses this alert class permanently",
            source, event_type, last - now, last.isoformat(),
        )
        return None
    if now - last > _COOLDOWN_MAX_AGE:
        logger.warning(
            "cooldown %s/%s is %s old (%s) — ignoring it as garbage",
            source, event_type, now - last, last.isoformat(),
        )
        return None
    return last


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

        Fails open in BOTH directions, which is the part that was missing: a
        store that is unreadable returns None (= "no record", alert allowed
        through), and so does a stored value we don't believe. Only a plausible
        stamp is ever cached — a poisoned one must not survive in memory after
        the document itself has been overwritten."""
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
        last = _validated_last_sent(
            (doc or {}).get("last_sent_at"),
            source=source,
            event_type=event_type,
            now=datetime.now(timezone.utc),
        )
        if last is not None:
            self._cooldowns[(source, event_type)] = last
        return last

    async def _can_send(self, source: str, event_type: str, cooldown_seconds: int) -> bool:
        # cooldown_seconds <= 0 means "always send" — don't let same-granularity
        # repeats get suppressed by the >= comparison.
        if cooldown_seconds <= 0:
            return True
        key = (source, event_type)
        now = datetime.now(timezone.utc)
        last_sent = self._cooldowns.get(key)
        if last_sent is None:
            last_sent = await self._load_cooldown(source, event_type)
        elif _validated_last_sent(last_sent, source=source, event_type=event_type, now=now) is None:
            # Re-checked on the cache hit too: _mark_sent is not the only writer
            # of this dict, and a value that would suppress forever must not get
            # a free pass just because it is already in memory.
            self._cooldowns.pop(key, None)
            return True
        if last_sent is None:
            return True
        return now - last_sent >= timedelta(seconds=cooldown_seconds)

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

    # ------------------------------------------------------------------ dedup

    @staticmethod
    async def _apply_severity(db, doc: dict, severity: str, now: datetime) -> dict:
        """Reconcile an open row's severity with a repeat that just merged into
        it, and keep its expiry consistent with the result.

        Severity MUST be able to escalate. Freezing it at the first occurrence
        meant an unprivileged write could cap it: `POST /notifications/send
        {"source":"relay","event_type":"dead"}` needs only the global API key and
        pre-creates the `relay|dead` row at whatever classify() decides on its
        own; the RelayWatchdog's real `critical` then deduped into that row and
        changed nothing, so a query for `severity=critical` returned no rows at
        all — the break-glass escalation muted by the cheapest write on the box,
        with no privileged access anywhere. The raise is a separate conditional
        update because it cannot ride the `$max` above: BSON compares the
        severity STRING alphabetically, where "critical" loses to "info", "low"
        and "medium". The `$in` filter is the concurrency guard — racing repeats
        can only ever move a row up.

        `kind` deliberately does NOT escalate. It is the row's routing identity:
        the break-glass allow-list is keyed on it, the triage worker selects on
        it, and the digest groups by it. Rewriting it under a consumer that has
        already selected the row moves an alert between lanes mid-flight, which
        is a different bug from the one being fixed here. A recurrence that is
        genuinely a different kind of thing is a different alert — it carries its
        own dedup_key (callers that need this already pass one).
        """
        current = severity_rank(doc.get("severity"))
        if current >= severity_rank(severity):
            # No raise. An info row that is still recurring should still live a
            # week past its LAST occurrence, not its first.
            if doc.get("expires_at") is not None:
                try:
                    await db.alerts.update_one(
                        {"_id": doc.get("_id")},
                        {"$set": {"expires_at": _expiry(doc.get("severity"), now)}},
                    )
                except Exception as exc:
                    logger.debug("alert expiry refresh failed (%s): %s", doc.get("dedup_key"), exc)
            return doc
        # None covers a pre-v2 row with no severity field at all.
        weaker = [None, *SEVERITIES[: severity_rank(severity)]]
        try:
            from pymongo import ReturnDocument

            raised = await db.alerts.find_one_and_update(
                {"_id": doc.get("_id"), "severity": {"$in": weaker}},
                # An escalated row stops being disposable: clearing expires_at
                # keeps the TTL sweep off a row that now matters.
                {"$set": {"severity": severity, "expires_at": _expiry(severity, now)}},
                return_document=ReturnDocument.AFTER,
            )
        except Exception as exc:
            logger.warning(
                "alert severity escalation failed (%s -> %s): %s",
                doc.get("dedup_key"), severity, exc,
            )
            return doc
        if raised:
            logger.info(
                "alert %s escalated %s -> %s", doc.get("dedup_key"), doc.get("severity"), severity
            )
        return raised or doc

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
        second row (returns deduped=True) — and may raise that row's severity,
        never lower it."""
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

        # Resolved against db.projects, not guessed from the basename — needs the
        # db, which is why it happens here rather than beside the other derived
        # fields above.
        if not project_slug:
            project_slug = await resolve_project_slug(db, project_path)

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
            if existing is not None:
                existing = await self._apply_severity(db, existing, severity, now)
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
                "severity": existing.get("severity", severity),
                "needs_human": bool(existing.get("needs_human", needs_human)),
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
            # TTL field (index in main.py). Null for anything above info, so the
            # sweep can only ever reap the record-keeping lane.
            "expires_at": _expiry(severity, now),
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
