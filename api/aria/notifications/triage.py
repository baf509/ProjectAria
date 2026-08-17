"""
ARIA - Alert Triage

Purpose: classify un-acked raises, diagnose the real failures with a bounded,
diagnose-only coding session, and write a *proposal* onto the alert — never
apply it.

This logic used to be a Hermes cron prompt ("ARIA alert triage", hourly,
executed by a 4B model on gemma :8104): classify INFORMATIONAL vs FAILURE → ack
first → spawn a DIAGNOSE-ONLY claude_code session → poll 6 × 20 s → always stop
it → send one Signal message ending "Reply APPLY". On 2026-08-10 that model went
away, the job raised `RuntimeError: Connection error`, the gateway paused it, and
nobody noticed for five days — the same failure class that killed the relay three
times. So the loop moves into ARIA code, where it is tested, stateful and
idempotent, and where a dead classifier costs a *downgrade*, never a delivery
(delivery is now the LLM-free Hermes outbox reading `needs_human=true`).

Three things are deliberately different from the cron:

1. **Failures are never acked.** The cron acked first so its own re-listing
   wouldn't re-triage the same row; ARIA keeps a per-alert `triage` record
   instead. Acking a real failure here would drop it out of the outbox's
   `needs_human=true & unacked` selection — i.e. triage would silence the very
   alert it was diagnosing.
2. **An unusable classification leaves the alert alone.** Qwen3.8 is a reasoning
   model: it emits `reasoning_content` first, and a tight budget returns
   finish_reason="length" with an EMPTY content string (this is exactly how DS4
   silently labelled every memory with zero entities — CLAUDE.md, Ontology
   Memory Map). Empty or ambiguous ⇒ no change ⇒ Ben still gets the alert.
3. **DIAGNOSE-ONLY is enforced structurally, not by asking politely.** The
   session is stopped in a `finally` (shielded, so even a shutdown lands the
   kill), the deadline is a clock and not a token count, and the workspace diff
   is hashed before and after — an agent that edited files gets its proposal
   withheld and raises a guard event instead.

Related: steward proposal §1.2, §3.1 item 12, §6.3.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from aria.config import settings

logger = logging.getLogger(__name__)

TRIAGE_RUNS_COLLECTION = "triage_runs"

# Source prefix of every alert this module raises. Also the first deny rule:
# triage must never triage its own output (the cron's diagnostic sessions
# emitting alerts that spawned more diagnostic sessions is how 31 rows piled up).
TRIAGE_SOURCE = "triage"

# Kinds a background worker must never reclassify or hand to an agent. A tamper
# or e-stop event is precisely the case where "let a coding agent look into it"
# is the wrong instinct (principle 12: the evaluator and the kill switch are
# unwritable by the thing they evaluate).
DENY_KINDS = ("guard", "estop", "killswitch", "tamper")

# Sources that are already a decision for Ben and have nothing an agent can add.
# `shells:nudge` (a watched shell stayed paused through three nudges) was carved
# out of the cron's diagnose step for exactly this reason: the shell is waiting
# for a human instruction, so spawning a cloud session to "diagnose" it burns a
# session to tell Ben what the alert already said.
DENY_SOURCES = ("shells:nudge",)

_TERMINAL_SESSION_STATUS = ("completed", "failed", "stopped", "error", "cancelled")

# The agent's answer is fenced so it can be told apart from the prompt echo that
# shares the pane with it. Parsing takes the LAST complete block for the same
# reason.
REPORT_BEGIN = "TRIAGE_REPORT_BEGIN"
REPORT_END = "TRIAGE_REPORT_END"

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_OPEN_THINK_RE = re.compile(r"<think>.*\Z", re.S | re.I)
_FIELD_RE = re.compile(
    r"^\s*(ROOT_CAUSE|FIX|CONFIDENCE|EVIDENCE)\s*:\s*(.*)$", re.I
)
_INFORMATIONAL = "INFORMATIONAL"
_FAILURE = "FAILURE"

_MAX_ROOT_CAUSE = 600
_MAX_FIX = 1200
_MAX_EVIDENCE = 800
_MAX_ALERT_TEXT = 1500

CLASSIFY_SYSTEM = (
    "You triage machine alerts for a single-user Linux host. Answer with ONE "
    "word on the first line: INFORMATIONAL or FAILURE. INFORMATIONAL means a "
    "routine status roll-up — a weekly/daily report, a summary, a count, a "
    "healthy-services listing, a recovery notice, anything whose leading marker "
    "is a success mark. FAILURE means the alert names an error, crash, restart "
    "loop, timeout, unreachable service, or a degraded/failed check. If you are "
    "not sure, answer FAILURE. Then one short line explaining why."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def strip_reasoning(text: str) -> str:
    """Drop `<think>…</think>` spans the OpenAI-compatible adapter wraps around
    a reasoning model's `reasoning_content` (llm/openai.py). An unterminated
    block means the answer was cut off mid-thought — everything from `<think>`
    on is discarded, which leaves "" and is treated as a failure by the caller,
    not as a verdict."""
    if not text:
        return ""
    text = _THINK_RE.sub(" ", text)
    text = _OPEN_THINK_RE.sub(" ", text)
    return text.strip()


def parse_classification(raw: str) -> Optional[bool]:
    """True = informational, False = real failure, None = unusable.

    None is the load-bearing case: an empty completion, a refusal, or a reply
    naming both words must leave the alert untouched. Failing safe here means
    Ben still gets the alert; failing "helpfully" means he silently doesn't.
    """
    text = strip_reasoning(raw or "")
    if not text:
        return None
    upper = text.upper()
    has_info = _INFORMATIONAL in upper
    has_fail = _FAILURE in upper
    if has_info == has_fail:  # neither, or both — no verdict
        return None
    return has_info


def _is_placeholder(value: str) -> bool:
    """The prompt's own template lines can echo into the pane. A value that is
    still a placeholder (`<one sentence>`) is not an answer."""
    v = (value or "").strip()
    if not v:
        return True
    return v.startswith("<") and v.endswith(">")


def parse_proposal(raw: str) -> Optional[dict]:
    """Extract {root_cause, fix, confidence, evidence} from the agent's report.

    Reads the LAST complete BEGIN/END block: the pane holds our own prompt
    (which contains the template) above the agent's answer. Returns None unless
    a real root cause is present — writing an empty proposal would put a blank
    "ROOT CAUSE:" line in Ben's Signal message and look like an answer.
    """
    text = strip_reasoning(raw or "")
    if not text:
        return None
    blocks = []
    start = None
    for idx, line in enumerate(text.splitlines()):
        if REPORT_BEGIN in line:
            start = idx
        elif REPORT_END in line and start is not None:
            blocks.append((start, idx))
            start = None
    if not blocks:
        return None
    lines = text.splitlines()
    first, last = blocks[-1]
    fields: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in lines[first + 1 : last]:
        match = _FIELD_RE.match(line)
        if match:
            current = match.group(1).upper()
            fields[current] = [match.group(2).strip()]
        elif current:
            fields[current].append(line.strip())

    def _join(key: str, limit: int) -> str:
        parts = [p for p in fields.get(key, []) if p]
        return " ".join(parts).strip()[:limit]

    root_cause = _join("ROOT_CAUSE", _MAX_ROOT_CAUSE)
    if _is_placeholder(root_cause):
        return None
    confidence = _join("CONFIDENCE", 40).lower() or "unknown"
    if confidence not in ("high", "medium", "low"):
        # Keep whatever it said rather than inventing a level — a made-up
        # confidence is worse than an honest "unknown".
        confidence = confidence.split()[0] if confidence else "unknown"
    fix = _join("FIX", _MAX_FIX)
    evidence = _join("EVIDENCE", _MAX_EVIDENCE)
    return {
        "root_cause": root_cause,
        "fix": "" if _is_placeholder(fix) else fix,
        "confidence": confidence,
        "evidence": "" if _is_placeholder(evidence) else evidence,
    }


def build_diagnose_prompt(alert: dict) -> str:
    """The DIAGNOSE-ONLY contract, with the alert copied verbatim.

    No port table: the retired Hermes skill hard-coded ":8102 chadrock and :8095
    laguna are deliberately STOPPED", which went stale the moment the topology
    changed and taught the diagnosing agent wrong facts. `/infrastructure/running`
    is the truth and is asked for instead.
    """
    text = (alert.get("message") or alert.get("detail") or "").strip()[:_MAX_ALERT_TEXT]
    source = alert.get("source") or "?"
    event_type = alert.get("event_type") or "?"
    return f"""DIAGNOSE ONLY. You are investigating an ARIA alert. You must NOT
change, restart, fix, deploy or configure anything: no file edits, no git
commands that write, no systemctl/docker, no service restarts. Read-only
commands only. A write is a contract violation and is detected.

The alert, verbatim:
---
source: {source}
event_type: {event_type}
{text}
---

Useful read-only sources on this host:
  journalctl --user -u aria-api --since '30 min ago'
  curl -s localhost:8200/api/v1/health
  curl -s localhost:8200/api/v1/infrastructure/running   # what is meant to be
      running, both registries — a server listed as stopped on purpose is NOT
      an incident
  the repo's own code and docs (read them; do not edit them)

When you are done, print exactly this block, once, as your last output:

{REPORT_BEGIN}
ROOT_CAUSE: <one or two sentences>
FIX: <the exact commands or edits a human would apply>
CONFIDENCE: <high|medium|low>
EVIDENCE: <the specific log line, status or file that proves it>
{REPORT_END}

If you cannot determine the cause, say so in ROOT_CAUSE and set CONFIDENCE: low.
Do not apply the fix. Someone else decides whether it is applied."""


async def _stops_engaged(db) -> bool:
    """True while the killswitch or e-stop is engaged. Uses the existing gates
    (deps.get_killswitch / resolve_estop_manager) — the same ones the reaper,
    the Ralph loop and the nudge route honour. Fails CLOSED: if the gate cannot
    be read, treat it as engaged, because the alternative is spawning agents
    during an emergency stop."""
    from aria.api.deps import get_killswitch, resolve_estop_manager

    try:
        if get_killswitch().is_active:
            return True
        estop = await resolve_estop_manager(db)
        return await estop.is_active()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("triage: safety gate unreadable, treating as engaged: %s", exc)
        return True


class TriageWorker:
    """Timer worker: classify raises, diagnose failures, propose — never apply."""

    def __init__(
        self,
        db,
        notifier=None,
        *,
        manager=None,
        adapter=None,
        interval_seconds: Optional[int] = None,
        max_alerts_per_tick: Optional[int] = None,
        max_diagnoses_per_hour: Optional[int] = None,
        max_attempts: Optional[int] = None,
        diagnose_enabled: Optional[bool] = None,
        diagnose_workspace: Optional[str] = None,
        diagnose_backend: Optional[str] = None,
        diagnose_model: Optional[str] = None,
        deadline_seconds: Optional[int] = None,
        poll_seconds: Optional[int] = None,
        use_worktree: Optional[bool] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.db = db
        self.notifier = notifier
        self._manager = manager
        self._adapter = adapter
        self.interval = max(
            15, int(_setting(interval_seconds, "triage_interval_seconds", 120))
        )
        self.max_alerts_per_tick = max(
            1, int(_setting(max_alerts_per_tick, "triage_max_alerts_per_tick", 3))
        )
        self.max_diagnoses_per_hour = int(
            _setting(max_diagnoses_per_hour, "triage_max_diagnoses_per_hour", 4)
        )
        self.max_attempts = max(
            1, int(_setting(max_attempts, "triage_max_attempts", 2))
        )
        self.diagnose_enabled = bool(
            _setting(diagnose_enabled, "triage_diagnose_enabled", True)
        )
        self.diagnose_workspace = str(
            _setting(
                diagnose_workspace,
                "triage_diagnose_workspace",
                "/home/ben/Development/ProjectAria",
            )
        )
        self.diagnose_backend = str(
            _setting(diagnose_backend, "triage_diagnose_backend", "claude_code")
        )
        self.diagnose_model = _setting(diagnose_model, "triage_diagnose_model", "") or None
        self.deadline_seconds = max(
            0, int(_setting(deadline_seconds, "triage_diagnose_deadline_seconds", 120))
        )
        self.poll_seconds = max(
            0, int(_setting(poll_seconds, "triage_diagnose_poll_seconds", 20))
        )
        # Isolation follows the guard's policy rather than triage's opinion: if
        # every ARIA session gets a worktree, so does this one, and the write
        # tripwire below reads the cleaner of the two signals.
        self.use_worktree = bool(
            _setting(use_worktree, "guard_worktree_default", False)
        )
        self._now = now or _utcnow
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last: dict = {"checked_at": None, "reason": "not_run"}

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        if not getattr(settings, "triage_enabled", False):
            logger.info("triage worker disabled by settings")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="notifications.triage")
        logger.info(
            "triage worker started (every %ds, %d diagnoses/hour max, deadline %ds)",
            self.interval, self.max_diagnoses_per_hour, self.deadline_seconds,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    def status(self) -> dict:
        return {
            "enabled": bool(getattr(settings, "triage_enabled", False)),
            "running": self._task is not None and not self._task.done(),
            "interval_seconds": self.interval,
            "max_diagnoses_per_hour": self.max_diagnoses_per_hour,
            "diagnose_enabled": self.diagnose_enabled,
            "last": dict(self._last),
        }

    async def _run(self) -> None:
        # Settle before the first tick: on boot the alert queue is full of rows
        # from before the restart and Mongo/model endpoints may not be up yet.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("triage tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ tick

    async def tick_once(self) -> dict:
        """One triage pass. Returns a summary; separated from the loop so it can
        be unit-tested and driven manually."""
        now = self._now()
        summary: dict[str, Any] = {
            "checked_at": now,
            "considered": 0,
            "informational": 0,
            "diagnosed": 0,
            "skipped": 0,
            "failed": 0,
        }
        if await _stops_engaged(self.db):
            summary["reason"] = "stop_engaged"
            self._last = summary
            return summary

        candidates = await self._candidates()
        for alert in candidates[: self.max_alerts_per_tick]:
            summary["considered"] += 1
            try:
                outcome = await self.triage_alert(alert)
            except Exception as exc:  # pragma: no cover - per-alert isolation
                logger.warning(
                    "triage of alert %s failed: %s", alert.get("_id"), exc
                )
                outcome = "error"
            if outcome == "informational":
                summary["informational"] += 1
            elif outcome == "diagnosed":
                summary["diagnosed"] += 1
            elif outcome in ("skipped", "denied", "rate_limited", "stop_engaged"):
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
            if self._stop.is_set():
                break
        summary["reason"] = "ok"
        self._last = summary
        return summary

    async def _candidates(self) -> list[dict]:
        """Un-acked raises with no proposal yet, newest first.

        Filtering happens in Python rather than in the query: `triage.attempts`
        and the deny rules are cheap to evaluate over a queue this size (tens of
        rows), and a mis-typed Mongo filter here would silently triage nothing —
        the failure mode this whole module exists to end.
        """
        cursor = (
            self.db.alerts.find({"acked": False, "needs_human": True})
            .sort("created_at", -1)
            .limit(50)
        )
        rows = [doc async for doc in cursor]
        out = []
        for doc in rows:
            if doc.get("proposal"):
                continue
            state = (doc.get("triage") or {})
            if state.get("state") in ("informational", "diagnosed", "denied"):
                continue
            if int(state.get("attempts") or 0) >= self.max_attempts:
                continue
            out.append(doc)
        return out

    def _denied(self, alert: dict) -> Optional[str]:
        source = (alert.get("source") or "").lower()
        kind = (alert.get("kind") or "").lower()
        if source.startswith(TRIAGE_SOURCE):
            return "own_output"
        if kind in DENY_KINDS or source in DENY_KINDS:
            return "protected_kind"
        if source in DENY_SOURCES:
            return "needs_human_instruction"
        return None

    async def triage_alert(self, alert: dict) -> str:
        """Classify one alert and, if it is a real failure, diagnose it.
        Returns the outcome slug written to the alert's `triage` record."""
        denied = self._denied(alert)
        if denied:
            await self._record_triage(alert, "denied", reason=denied, bump=False)
            return "denied"

        prior = alert.get("triage") or {}
        severity = (alert.get("severity") or "").lower()
        if prior.get("informational") is False:
            # Already classified as a failure on an earlier tick and deferred
            # (budget spent, stop engaged). Re-asking the model would spend a
            # Qwen slot-2 call per tick to learn the same thing.
            informational = False
            reason = prior.get("reason") or "previously classified as a failure"
            model_used = prior.get("model")
        elif severity == "critical":
            # A 27B classifier does not get to decide that a critical row is
            # routine. Criticals go straight to diagnosis (or to nothing) and
            # keep needs_human either way.
            informational = False
            reason = "critical severity is never reclassified"
            model_used = None
        else:
            verdict = await self._classify(alert)
            if verdict is None:
                await self._record_triage(
                    alert, "classify_failed", reason="model gave no usable verdict"
                )
                return "failed"
            informational, reason, model_used = verdict

        if informational:
            await self._downgrade(alert, reason, model_used)
            return "informational"

        if not self.diagnose_enabled:
            await self._record_triage(
                alert, "classified", reason=reason, informational=False, model=model_used
            )
            return "skipped"
        if await self._diagnoses_last_hour() >= self.max_diagnoses_per_hour:
            logger.info("triage: diagnosis budget spent for this hour; deferring")
            await self._record_triage(
                alert, "deferred", reason="diagnosis budget spent", bump=False,
                informational=False, model=model_used,
            )
            return "rate_limited"
        # Re-check immediately before spawning: a stop may have engaged during
        # classification, and start_session's own gate raising would burn an
        # attempt on an alert that was never actually triaged.
        if await _stops_engaged(self.db):
            await self._record_triage(
                alert, "deferred", reason="killswitch/e-stop engaged", bump=False,
                informational=False, model=model_used,
            )
            return "stop_engaged"

        proposal = await self._diagnose(alert)
        if not proposal:
            await self._record_triage(
                alert, "diagnose_failed", reason="no usable report from the session"
            )
            return "failed"
        await self._attach_proposal(alert, proposal)
        return "diagnosed"

    # -------------------------------------------------------------- classify

    async def _classify(self, alert: dict) -> Optional[tuple[bool, str, Optional[str]]]:
        """Ask the local model whether this alert is informational.

        Returns None on anything unusable — an unreachable endpoint, an empty
        completion (the reasoning-model trap), or a reply naming both verdicts.
        """
        adapter = self._resolve_adapter()
        if adapter is None:
            return None
        text = (alert.get("message") or alert.get("detail") or "")[:_MAX_ALERT_TEXT]
        user = (
            f"source: {alert.get('source')}\n"
            f"event_type: {alert.get('event_type')}\n"
            f"severity: {alert.get('severity')}\n"
            f"occurrences: {alert.get('occurrences')}\n\n{text}"
        )
        model = getattr(settings, "triage_classify_model", None) or getattr(settings, "steward_model", None)
        try:
            from aria.llm.base import Message

            content, _tool_calls, _usage = await asyncio.wait_for(
                adapter.complete(
                    [
                        Message(role="system", content=CLASSIFY_SYSTEM),
                        Message(role="user", content=user),
                    ],
                    temperature=0.0,
                    # Generous on purpose: a reasoning model spends most of a
                    # tight budget in reasoning_content and then returns an
                    # empty `content` with finish_reason="length".
                    max_tokens=int(getattr(settings, "triage_classify_max_tokens", 512)),
                ),
                timeout=float(getattr(settings, "triage_classify_timeout_seconds", 120)),
            )
        except Exception as exc:
            logger.warning("triage: classification call failed: %s", exc)
            return None
        verdict = parse_classification(content)
        if verdict is None:
            logger.warning(
                "triage: unusable classification for alert %s (%d chars of content)",
                alert.get("_id"), len(content or ""),
            )
            return None
        reason = strip_reasoning(content).splitlines()
        detail = " ".join(line.strip() for line in reason[1:] if line.strip())[:300]
        return verdict, detail or ("informational" if verdict else "failure"), model

    def _resolve_adapter(self):
        if self._adapter is not None:
            return self._adapter
        try:
            from aria.llm.manager import LLMManager

            # 2026-08-17: classification runs on its OWN model (gemma :8104),
            # not steward_model — see config.triage_classify_*. steward_model
            # also drives the Steward service and agents/review.py, so reusing
            # it here would have coupled three unrelated consumers.
            self._adapter = LLMManager().get_adapter(
                getattr(settings, "triage_classify_backend", None)
                or getattr(settings, "steward_backend", "llamacpp"),
                getattr(settings, "triage_classify_model", None)
                or getattr(settings, "steward_model", "default"),
                base_url=(getattr(settings, "triage_classify_endpoint", None)
                          or getattr(settings, "steward_endpoint", None) or None),
            )
        except Exception as exc:
            logger.warning("triage: no classifier adapter available: %s", exc)
            self._adapter = None
        return self._adapter

    # -------------------------------------------------------------- diagnose

    async def _diagnose(self, alert: dict) -> Optional[dict]:
        """Spawn a diagnose-only session, poll to a deadline, ALWAYS stop it.

        The budget is debited before the spawn: a crash between spawn and
        bookkeeping must not hand out a free session, or a crash loop becomes an
        unbounded cloud spend.
        """
        manager = await self._resolve_manager()
        if manager is None:
            return None
        alert_id = str(alert.get("_id"))
        run = {
            "kind": "diagnose",
            "alert_id": alert_id,
            "started_at": self._now(),
            "finished_at": None,
            "session_id": None,
            "outcome": "started",
            "backend": self.diagnose_backend,
        }
        run_id = await self._insert_run(run)

        use_worktree = self.use_worktree
        # Baseline for the write tripwire. With a worktree the session starts
        # from a clean tree, so the baseline is the empty diff; without one the
        # live checkout is usually already dirty (Ben's own work) and only a
        # CHANGE counts.
        before = "" if use_worktree else await self._diff_hash(self.diagnose_workspace)
        session_id = None
        output = ""
        try:
            session = await manager.start_session(
                workspace=self.diagnose_workspace,
                backend=self.diagnose_backend,
                prompt=build_diagnose_prompt(alert),
                model=self.diagnose_model,
                create_worktree=use_worktree,
                visible=False,
            )
            session_id = (session or {}).get("_id") or (session or {}).get("id")
            if not session_id:
                raise RuntimeError("start_session returned no session id")
            await self._update_run(run_id, {"session_id": session_id})
            output = await self._poll(manager, session_id)
        except Exception as exc:
            logger.warning("triage: diagnosis for alert %s failed: %s", alert_id, exc)
            await self._update_run(
                run_id, {"outcome": "error", "error": str(exc)[:300], "finished_at": self._now()}
            )
            return None
        finally:
            if session_id:
                await self._always_stop(manager, session_id)

        wrote = await self._check_wrote(manager, session_id, before, use_worktree)
        if wrote:
            await self._raise_write_violation(alert, session_id)
            await self._update_run(
                run_id,
                {"outcome": "wrote_to_workspace", "finished_at": self._now()},
            )
            return None

        proposal = parse_proposal(output)
        if not proposal:
            await self._update_run(
                run_id, {"outcome": "unparseable", "finished_at": self._now()}
            )
            return None
        proposal.update(
            {
                "by": "triage",
                "session_id": session_id,
                "at": self._now(),
                "backend": self.diagnose_backend,
            }
        )
        await self._update_run(
            run_id, {"outcome": "proposed", "finished_at": self._now()}
        )
        return proposal

    async def _poll(self, manager, session_id: str) -> str:
        """Read the pane until the report appears, the session ends, or the
        deadline passes. The deadline is wall-clock: the cron's "6 × 20 s" was
        the model counting its own polls, which it sometimes didn't."""
        deadline = self._now() + timedelta(seconds=self.deadline_seconds)
        output = ""
        while self._now() < deadline and not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                break  # stop requested
            except asyncio.TimeoutError:
                pass
            try:
                output = await manager.get_output(session_id, lines=250)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("triage: get_output failed for %s: %s", session_id, exc)
                continue
            if parse_proposal(output):
                return output
            try:
                session = await manager.get_session(session_id)
            except Exception:  # pragma: no cover - defensive
                session = None
            if session and session.get("status") in _TERMINAL_SESSION_STATUS:
                break
        return output

    async def _always_stop(self, manager, session_id: str) -> None:
        """Stop the diagnostic session however we got here.

        The cron asked the model to "ALWAYS call stop_coding_session" and leaked
        sessions whenever it timed out. Here it is a finally-block, and the stop
        is shielded so even a cancelled worker (aria-api shutting down) lands the
        kill instead of abandoning a running agent.
        """
        task = asyncio.ensure_future(self._stop_quiet(manager, session_id))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning(
                "triage: stop of diagnostic session %s did not confirm in time",
                session_id,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("triage: stop of session %s raised: %s", session_id, exc)

    @staticmethod
    async def _stop_quiet(manager, session_id: str) -> None:
        try:
            await manager.stop_session(session_id)
        except Exception as exc:
            logger.warning("triage: stop_session(%s) failed: %s", session_id, exc)

    async def _check_wrote(
        self, manager, session_id: Optional[str], before: Optional[str], use_worktree: bool
    ) -> bool:
        """Did the diagnose-only session modify the tree it ran in?

        A `None` baseline (workspace is not a git repo, git unavailable) means
        the tripwire cannot speak, and silence is not an accusation — the check
        is skipped rather than guessed.
        """
        if not session_id or before is None:
            return False
        try:
            after = await manager.get_diff(session_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("triage: post-run diff failed for %s: %s", session_id, exc)
            return False
        after_hash = _hash(after or "")
        if use_worktree:
            return bool((after or "").strip())
        return after_hash != before

    async def _diff_hash(self, workspace: str) -> Optional[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", workspace, "diff", "--no-ext-diff",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception as exc:
            logger.debug("triage: baseline diff of %s unavailable: %s", workspace, exc)
            return None
        if proc.returncode != 0:
            return None
        return _hash(stdout.decode("utf-8", errors="replace"))

    async def _raise_write_violation(self, alert: dict, session_id: Optional[str]) -> None:
        """A diagnostic agent that edited files broke its contract. Ben hears
        about the write, not about the analysis — and the analysis is withheld,
        because an agent that ignored the one rule it was given is not a source
        to quote in a proposal."""
        detail = (
            f"Diagnostic session {session_id} modified {self.diagnose_workspace} while "
            f"triaging alert {alert.get('_id')} — it was told DIAGNOSE ONLY. The proposal "
            f"was discarded; nothing was applied. Inspect the workspace before trusting it."
        )
        try:
            from aria.guard.policy import record_event

            await record_event(
                self.db,
                "diagnose_write",
                detail,
                session_id=session_id,
                path=self.diagnose_workspace,
                blocked=False,
                severity="critical",
                actor="triage",
            )
        except Exception as exc:  # pragma: no cover - guard is optional here
            logger.warning("triage: could not record guard event: %s", exc)
        if self.notifier:
            try:
                await self.notifier.notify(
                    source=f"{TRIAGE_SOURCE}:diagnose",
                    event_type="diagnose:wrote_to_workspace",
                    detail=detail,
                    severity="high",
                    kind=TRIAGE_SOURCE,
                    needs_human=True,
                    dedup_key=f"triage|wrote|{session_id}",
                    cooldown_seconds=0,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("triage: violation alert failed: %s", exc)

    # ------------------------------------------------------------ alert writes

    async def _downgrade(self, alert: dict, reason: str, model: Optional[str]) -> None:
        """Informational → info severity, needs_human off, acked with a reason.

        Only these three fields plus `triage` are written: `decision` is Ben's,
        `detail`/`message` are the emitter's (S3 ownership — a worker never
        clobbers a field it does not own).
        """
        now = self._now()
        await self.db.alerts.update_one(
            {"_id": alert.get("_id")},
            {
                "$set": {
                    "severity": "info",
                    "needs_human": False,
                    "acked": True,
                    "acked_at": now,
                    "triage": {
                        "state": "informational",
                        "reason": reason,
                        "model": model,
                        "at": now,
                        "attempts": int((alert.get("triage") or {}).get("attempts") or 0),
                    },
                }
            },
        )
        logger.info("triage: alert %s downgraded to info (%s)", alert.get("_id"), reason)

    async def _attach_proposal(self, alert: dict, proposal: dict) -> None:
        """Write the proposal and STOP. Applying it is Ben's `APPLY <id>`, which
        arrives as decide_alert — the whole point of propose-don't-act. The alert
        keeps needs_human=true so the outbox still delivers it, now with a root
        cause attached."""
        now = self._now()
        await self.db.alerts.update_one(
            {"_id": alert.get("_id")},
            {
                "$set": {
                    "proposal": proposal,
                    "triage": {
                        "state": "diagnosed",
                        "reason": "proposal written",
                        "at": now,
                        "session_id": proposal.get("session_id"),
                        "attempts": int((alert.get("triage") or {}).get("attempts") or 0) + 1,
                    },
                }
            },
        )
        logger.info(
            "triage: alert %s has a proposal (confidence=%s)",
            alert.get("_id"), proposal.get("confidence"),
        )

    async def _record_triage(
        self,
        alert: dict,
        state: str,
        *,
        reason: str = "",
        bump: bool = True,
        informational: Optional[bool] = None,
        model: Optional[str] = None,
    ) -> None:
        """Persist what triage did with this alert. `bump=False` for outcomes
        that are not the alert's fault (budget spent, stop engaged) — burning an
        attempt on those would retire an alert nobody ever looked at."""
        attempts = int((alert.get("triage") or {}).get("attempts") or 0)
        if bump:
            attempts += 1
        record = {
            "state": state,
            "reason": reason,
            "at": self._now(),
            "attempts": attempts,
        }
        if informational is not None:
            record["informational"] = informational
        if model is not None:
            record["model"] = model
        await self.db.alerts.update_one(
            {"_id": alert.get("_id")}, {"$set": {"triage": record}}
        )

    # --------------------------------------------------------------- plumbing

    async def _resolve_manager(self):
        if self._manager is not None:
            return self._manager
        try:
            from aria.api.deps import get_coding_session_manager

            self._manager = await get_coding_session_manager(self.db)
        except Exception as exc:
            logger.warning("triage: coding session manager unavailable: %s", exc)
            self._manager = None
        return self._manager

    async def _diagnoses_last_hour(self) -> int:
        cutoff = self._now() - timedelta(hours=1)
        try:
            return int(
                await self.db[TRIAGE_RUNS_COLLECTION].count_documents(
                    {"kind": "diagnose", "started_at": {"$gte": cutoff}}
                )
            )
        except Exception as exc:
            # Fail CLOSED on an unreadable budget: an unknown spend is not a
            # licence to spend more.
            logger.warning("triage: diagnosis budget unreadable (%s); refusing", exc)
            return self.max_diagnoses_per_hour

    async def _insert_run(self, run: dict):
        try:
            result = await self.db[TRIAGE_RUNS_COLLECTION].insert_one(dict(run))
            return getattr(result, "inserted_id", None)
        except Exception as exc:  # pragma: no cover - telemetry never blocks
            logger.debug("triage: run insert failed: %s", exc)
            return None

    async def _update_run(self, run_id, fields: dict) -> None:
        if run_id is None:
            return
        try:
            await self.db[TRIAGE_RUNS_COLLECTION].update_one(
                {"_id": run_id}, {"$set": fields}
            )
        except Exception as exc:  # pragma: no cover - telemetry never blocks
            logger.debug("triage: run update failed: %s", exc)


def _setting(explicit, name: str, default):
    """Constructor argument wins, then the setting, then the built-in default.

    The settings are read through getattr so this module runs on a config that
    predates the `triage_*` block (see the integration spec) instead of failing
    at import.
    """
    if explicit is not None:
        return explicit
    value = getattr(settings, name, None)
    return default if value is None else value


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
