"""
ARIA - Outcome Scoring

Purpose: give every coding session a label derived from EVIDENCE, and expose the
metric set the weekly report is built from.

Why this exists: today nothing is measured. 14 of 17 coding sessions sit at
`stopped`, every `session_report` says `partial`, the C1 gate is off,
`result_summary` is never persisted, and `db.usage` has recorded nothing since
2026-07-30. A steward that cannot tell a good session from a bad one cannot
improve anything, so this module is the precondition for the whole
self-improvement phase — not an analytics extra.

The standing rule this module enforces: **a self-report is a claim, not a
confirmation.** `success` is never read from the agent's own summary, its exit
code, or its "RALPH_DONE". It is derived from things that happened outside the
agent: did the guard's merge land, did the C1 gate pass, is there a diff at all,
did an uncorrelated reviewer reject it, was anything rolled back or blocked.

`verified` is reported alongside `success` on purpose. A session with a diff but
no gate, no tests and no review is not a success — but it is also not the same
thing as a session that failed its gate, and averaging the two together would
make the "success rate" metric mean nothing. `success=False, verified=False`
reads "we do not know", and `metrics()` counts it separately.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from aria.config import settings
from aria.llm.pricing import cost_for

logger = logging.getLogger(__name__)

OUTCOMES_COLLECTION = "session_outcomes"
# Alias for the improver's defensive seam (steward/improve.py imports
# SESSION_OUTCOMES_COLLECTION). Two names for one collection is worse than one,
# but a rename that silently detaches the improver from its own baseline is
# worse still — so the alias exists and both point at the same constant.
SESSION_OUTCOMES_COLLECTION = OUTCOMES_COLLECTION

# String labels the improver's `_is_success` recognises. This module does NOT
# write a string label — it writes tri-state `success` (True / False / None) —
# so these exist only so the seam binds to one vocabulary instead of two
# diverging defaults.
SUCCESS_LABELS = ("success", "succeeded", "merged", "clean")

# Statuses a session can no longer move out of on its own. `stopped` is included
# deliberately: it is the single most common state on this box (14/17), and
# leaving it unscored is why nothing has ever been measured.
TERMINAL_STATUSES = ("completed", "failed", "stopped")

# Where pi streams its structured transcript. Per-turn token usage exists ONLY
# here — ARIA never sees pi's model calls, so without this file a local session's
# cost is unknowable.
PI_SESSIONS_ROOT = "~/.pi/agent/sessions"


# ---------------------------------------------------------------------------
# pi transcript — token attribution
# ---------------------------------------------------------------------------

def _load_pi_transcript_module():
    """The shared pi JSONL parser, when it exists.

    `steward/pi_transcript.py` is owned by the supervisor work in the same wave.
    Importing it defensively rather than as a hard dependency is what let this
    module land and be tested before it existed; now that it does, its
    `load_transcript()` is the preferred path and the scan below is only the
    degraded one. Both read the same `message.usage` fields, so the numbers do
    not move when one replaces the other.
    """
    try:
        from aria.steward import pi_transcript  # type: ignore
    except Exception:  # noqa: BLE001 — absent module must not break scoring
        return None
    return pi_transcript


# Other names the shared parser may grow, probed after `load_transcript`. A
# duck-typed probe rather than a hard import of one symbol: the contract needed
# here is narrow — "given a session id, give me token totals" — and coupling to
# an exact signature across a repo two agents are editing at once is how a
# rename turns into a silent zero.
_PI_USAGE_FUNCS = ("session_usage", "usage_for_session", "usage_totals", "parse_usage")


def _normalize_usage(raw: Any) -> Optional[dict]:
    """Accept the several spellings a usage payload arrives in."""
    if not isinstance(raw, dict):
        return None

    def pick(*names: str) -> int:
        for name in names:
            value = raw.get(name)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    tokens_in = pick("input_tokens", "tokens_in", "input", "prompt_tokens")
    tokens_out = pick("output_tokens", "tokens_out", "output", "completion_tokens")
    if not tokens_in and not tokens_out:
        return None
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cache_read": pick("cache_read", "cacheRead", "cache_read_tokens"),
        "cache_write": pick("cache_write", "cacheWrite", "cache_write_tokens"),
        "turns": pick("turns", "messages", "count"),
        "model": raw.get("model") if isinstance(raw.get("model"), str) else None,
        "backend": raw.get("provider") if isinstance(raw.get("provider"), str) else None,
    }


def find_pi_transcript(session_id: str, root: Optional[str] = None) -> Optional[str]:
    """The JSONL for an ARIA-spawned pi session.

    ARIA passes its own uuid as `--session-id`, and pi names the file
    `<ISO timestamp>_<session-id>.jsonl` under a cwd-derived directory — so the
    id suffix is the join key, and the directory is not worth reconstructing.
    """
    if not session_id:
        return None
    # Resolved here, not as a default argument: a default binds at import time,
    # which makes the root unpatchable and the lookup untestable.
    base = os.path.expanduser(root or PI_SESSIONS_ROOT)
    if not os.path.isdir(base):
        return None
    matches = glob.glob(os.path.join(base, "*", f"*{session_id}.jsonl"))
    matches += glob.glob(os.path.join(base, f"*{session_id}.jsonl"))
    return matches[0] if matches else None


def scan_pi_jsonl(path: str) -> Optional[dict]:
    """Sum `message.usage` across a pi transcript.

    Fallback for when `steward/pi_transcript.py` is absent. Deliberately tiny and
    tolerant: a truncated final line is normal in a live session, and one bad
    line must not cost the whole file's accounting.
    """
    totals = {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cache_write": 0, "turns": 0}
    model = backend = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                message = doc.get("message") if isinstance(doc, dict) else None
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                totals["tokens_in"] += int(usage.get("input") or 0)
                totals["tokens_out"] += int(usage.get("output") or 0)
                totals["cache_read"] += int(usage.get("cacheRead") or 0)
                totals["cache_write"] += int(usage.get("cacheWrite") or 0)
                totals["turns"] += 1
                model = message.get("model") or model
                backend = message.get("provider") or backend
    except OSError as exc:
        logger.debug("pi transcript unreadable (%s): %s", path, exc)
        return None
    if not totals["turns"]:
        return None
    totals["model"] = model
    totals["backend"] = backend
    return totals


async def _usage_from_transcript(module, session_id: str, workspace: Optional[str]):
    """Adapt `pi_transcript.load_transcript()` to the usage shape used here.

    The parser returns a rich `PiTranscript` (turns, tool calls, stuck signals);
    only the token totals and the model identity are wanted here, and they live
    on `.usage` / `.model` / `.provider`.
    """
    loader = getattr(module, "load_transcript", None)
    if loader is None:
        return None
    try:
        transcript = await loader(session_id, workspace)
    except TypeError:
        # Signature drift is survivable: try the one argument that is certain.
        transcript = await loader(session_id)
    if transcript is None:
        return None

    usage = getattr(transcript, "usage", None)
    payload = usage.to_dict() if hasattr(usage, "to_dict") else usage
    normalized = _normalize_usage(payload if isinstance(payload, dict) else {})
    if not normalized:
        return None
    normalized["model"] = getattr(transcript, "model", None) or normalized.get("model")
    normalized["backend"] = getattr(transcript, "provider", None) or normalized.get("backend")
    turns = getattr(transcript, "turns", None)
    if isinstance(turns, list):
        normalized["turns"] = len(turns)
    normalized["source"] = "pi_transcript.load_transcript"
    return normalized


async def pi_usage(
    session_id: str, *, workspace: Optional[str] = None, root: Optional[str] = None
) -> Optional[dict]:
    """Token totals for a pi session, from the shared parser or the fallback."""
    module = _load_pi_transcript_module()
    if module is not None:
        try:
            adapted = await _usage_from_transcript(module, session_id, workspace)
        except Exception as exc:  # noqa: BLE001 — fall through to the fallback
            logger.debug("pi_transcript.load_transcript failed for %s: %s", session_id, exc)
            adapted = None
        if adapted:
            return adapted
        for name in _PI_USAGE_FUNCS:
            func = getattr(module, name, None)
            if func is None:
                continue
            try:
                result = func(session_id)
                if asyncio.iscoroutine(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001 — fall through to the fallback
                logger.debug("pi_transcript.%s failed for %s: %s", name, session_id, exc)
                continue
            normalized = _normalize_usage(result)
            if normalized:
                normalized["source"] = f"pi_transcript.{name}"
                return normalized

    path = find_pi_transcript(session_id, root)
    if not path:
        return None
    totals = await asyncio.to_thread(scan_pi_jsonl, path)
    if not totals:
        return None
    totals["source"] = "jsonl_fallback"
    totals["path"] = path
    return totals


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_numstat(text: str) -> tuple[int, int]:
    """(files, lines) out of `git diff --numstat` output."""
    files = lines = 0
    for row in (text or "").splitlines():
        parts = row.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        for value in parts[:2]:
            if value.isdigit():
                lines += int(value)
    return files, lines


class OutcomeScorer:
    """Write one `session_outcomes` row per terminal coding session."""

    def __init__(self, db, notifier=None):
        self.db = db
        # Held, deliberately unused for alerting. Everything worth raising here
        # — a blocked action, a tamper hash, a rollback — is already raised by
        # the guard at the moment it happens, with the evidence attached. A
        # second alert minutes later from the scorer would be the same event
        # twice, and alert fatigue is the failure mode this whole layer exists
        # to avoid (proposal §6.3). It is kept for the digest, which reads
        # outcomes rather than being pushed them.
        self.notifier = notifier

    # -- public ---------------------------------------------------------

    async def score_session(
        self, session_id: str, *, force: bool = False
    ) -> Optional[dict]:
        """Score one session. Idempotent: re-scoring updates the same row.

        Returns None when the session is unknown or still running — a running
        session has no outcome yet, and guessing one is how `partial` became the
        only label in `session_reports`.
        """
        session = await self.db.coding_sessions.find_one({"_id": session_id})
        if not session:
            return None
        status = session.get("status")
        if status not in TERMINAL_STATUSES and not force:
            return None

        existing = await self._find_one(OUTCOMES_COLLECTION, {"session_id": session_id})
        if existing and not force:
            return existing

        outcome = await self.build_outcome(session)
        await self._store(outcome)
        return outcome

    async def score_pending(self, limit: int = 50) -> list[dict]:
        """Score every terminal session that has no outcome row yet.

        This is also the backfill: the 17 historical sessions get labels the
        first time it runs, which is what makes any baseline possible at all.
        """
        scored: list[dict] = []
        # Newest first, with a window far larger than this box's lifetime
        # session count (17 to date). Unsorted, a backlog of already-scored old
        # sessions would fill the window and starve every new one — the sweep
        # would look healthy and score nothing that matters.
        window = max(200, int(limit) * 10)
        try:
            sessions = await self.db.coding_sessions.find(
                {"status": {"$in": list(TERMINAL_STATUSES)}}
            ).sort("updated_at", -1).to_list(length=window)
        except Exception as exc:  # noqa: BLE001
            logger.warning("outcome scan failed: %s", exc)
            return scored

        for session in sessions:
            if len(scored) >= limit:
                break
            session_id = str(session.get("_id"))
            if await self._has_outcome(session_id):
                continue
            try:
                outcome = await self.build_outcome(session)
                await self._store(outcome)
                scored.append(outcome)
            except Exception as exc:  # noqa: BLE001 — one bad session must not stop the sweep
                logger.warning("could not score session %s: %s", session_id, exc)
        return scored

    # -- evidence -------------------------------------------------------

    async def build_outcome(self, session: dict) -> dict:
        """Assemble the outcome row from every independent source there is."""
        session_id = str(session.get("_id"))
        now = datetime.now(timezone.utc)

        guard = await self._find_one("guard_sessions", {"_id": session_id})
        gate = await self._latest("guard_gate_runs", {"session_id": session_id}, "at")
        events = await self._find("guard_events", {"session_id": session_id})
        report = await self._find_one("session_reports", {"session_id": session_id})
        review = await self._find_one("session_reviews", {"session_id": session_id})

        diff_files, diff_lines = self._diff_size(gate, report)
        gate_passed = self._gate_passed(gate, session)
        merged = any(e.get("kind") == "merge:done" for e in events)
        rolled_back = any(e.get("kind") == "session:rollback" for e in events)
        destructive_blocked = sum(1 for e in events if e.get("blocked"))

        created = _aware(session.get("created_at"))
        completed = _aware(session.get("completed_at")) or _aware(session.get("updated_at"))
        wall_seconds = (
            int((completed - created).total_seconds())
            if created and completed and completed >= created else None
        )
        first_diff_at = await self._first_diff_at(session_id)
        time_to_first_diff = (
            int((first_diff_at - created).total_seconds())
            if created and first_diff_at and first_diff_at >= created else None
        )

        usage = await self._attribute_usage(session)
        project_slug = await self._project_slug(session)
        charter = await self._charter(project_slug)

        from aria.agents.review import model_family
        from aria.agents.routing import classify_tier

        success, verified, reason = self._decide(
            session=session,
            gate_passed=gate_passed,
            diff_lines=diff_lines,
            merged=merged,
            rolled_back=rolled_back,
            review=review,
            report=report,
            wall_seconds=wall_seconds,
        )

        return {
            "session_id": session_id,
            "project_slug": project_slug,
            "backend": session.get("backend"),
            "llm": session.get("llm"),
            "model": session.get("model"),
            "family": model_family(
                session.get("backend"), session.get("model"), session.get("llm")
            ),
            # `tier` is the charter's vocabulary (local|ridge|red|cloud) because
            # that is what the ladder and the budget are expressed in;
            # `routing_tier` keeps the spawn-time complexity verdict, which
            # answers a different question (was this task deep or scoped).
            "tier": classify_tier(session),
            "routing_tier": ((session.get("routing") or {}).get("tier")),
            "autonomy": int((charter or {}).get("autonomy") or 0),
            "status": session.get("status"),
            "exit_code": session.get("exit_code"),
            "success": success,
            "verified": verified,
            "reason": reason,
            "gate_passed": gate_passed,
            "review_verdict": (review or {}).get("verdict"),
            "review_independent": (review or {}).get("independent"),
            "nudges": int(session.get("loop_nudges") or 0),
            "rungs_used": self._rungs_used(session),
            "wall_seconds": wall_seconds,
            "time_to_first_diff_seconds": time_to_first_diff,
            "tokens_in": usage.get("tokens_in"),
            "tokens_out": usage.get("tokens_out"),
            "cost_usd": usage.get("cost_usd"),
            "usage_source": usage.get("source"),
            "diff_lines": diff_lines,
            "diff_files": diff_files,
            "merged": merged,
            "rolled_back": rolled_back,
            "destructive_blocked": destructive_blocked,
            "branch": (guard or {}).get("branch"),
            "created_at": now,
            "session_started_at": created,
            "session_ended_at": completed,
        }

    # -- the label ------------------------------------------------------

    def _decide(
        self,
        *,
        session: dict,
        gate_passed: Optional[bool],
        diff_lines: int,
        merged: bool,
        rolled_back: bool,
        review: Optional[dict],
        report: Optional[dict],
        wall_seconds: Optional[int],
    ) -> tuple[Optional[bool], bool, str]:
        """(success, verified, reason) — evidence only, in falsifiability order.

        `success` is TRI-STATE: True, False, or None for "nothing checked this".
        None rather than False because every consumer averages this field —
        `metrics()` here, `collect_baseline()` in the improver — and counting an
        unchecked session as a failure would make the fleet's success rate a
        function of how many projects have a `check_command`, not of how well
        the agents work.

        Nothing here reads the agent's summary, its RALPH_DONE token, or its
        exit code as evidence of success. `exit_code == 0` appears once, as a
        *negative* signal (crash-as-completed), never as a positive one.
        """
        status = session.get("status")

        if merged and not rolled_back:
            return True, True, "merged by the guard after a passing gate"
        if rolled_back:
            return False, True, "rolled back"

        # Crash-as-completed: pi exiting in seconds with nothing to show is
        # currently recorded as `completed` (session.py:1008-1021). The clock
        # and the empty diff are both external facts, so this is decidable here.
        grace = int(getattr(settings, "meta_crash_grace_seconds", 60) or 60)
        if (
            wall_seconds is not None
            and wall_seconds < grace
            and diff_lines == 0
            and status != "stopped"
        ):
            return False, True, (
                f"exited after {wall_seconds}s with no diff — a crash reported as "
                f"'{status}'"
            )

        if gate_passed is False:
            return False, True, "C1 verification gate failed"

        if diff_lines == 0:
            if status == "stopped":
                return False, True, "stopped before producing a diff"
            return False, True, "no diff produced"

        if review and review.get("ran") and review.get("blocking"):
            return False, True, (
                f"different-family review rejected the diff: "
                f"{(review.get('summary') or '')[:160]}"
            )

        if gate_passed is True:
            return True, True, f"gate passed on a {diff_lines}-line diff"

        tests = (report or {}).get("tests") or {}
        if tests.get("ran"):
            if tests.get("success"):
                return True, True, (
                    f"{tests.get('command', 'tests')} passed on a {diff_lines}-line diff "
                    "(no project check_command configured)"
                )
            return False, True, f"{tests.get('command', 'tests')} failed"

        if status == "stopped":
            return False, True, f"stopped by an operator with {diff_lines} lines uncommitted"

        # A diff exists and nothing checked it. Not a success — and explicitly
        # not a measured failure: `success=None` / `verified=False` is what the
        # report counts as "unverified", and a rising count is the signal to
        # turn the gate on for that project rather than a regression.
        return None, False, (
            f"unverified: {diff_lines} lines changed, but no gate, tests or review ran"
        )

    # -- evidence helpers -----------------------------------------------

    @staticmethod
    def _gate_passed(gate: Optional[dict], session: dict) -> Optional[bool]:
        """Guard gate run first, then the watchdog's C1 `gate_runs` history.

        None means "no gate ran" and must never be conflated with False — the
        gate being off by default is the normal state on this box today.
        """
        if gate and "passed" in gate:
            return bool(gate.get("passed"))
        runs = session.get("gate_runs") or []
        if runs:
            return bool(runs[-1].get("passed"))
        return None

    @staticmethod
    def _diff_size(gate: Optional[dict], report: Optional[dict]) -> tuple[int, int]:
        """(files, lines). The guard's gate measured base..head, which survives
        the guard's own checkpoint commits; `git diff --numstat` in the report
        only ever saw uncommitted work."""
        for check in ((gate or {}).get("checks") or []):
            if check.get("name") == "diff_size" and "lines" in check:
                return int(check.get("files") or 0), int(check.get("lines") or 0)
        if gate and gate.get("changed_files"):
            return int(gate["changed_files"]), 0
        if report:
            return _parse_numstat(report.get("diff_numstat") or "")
        return 0, 0

    @staticmethod
    def _rungs_used(session: dict) -> int:
        """How far up the escalation ladder this session went.

        Reads whatever the supervisor recorded — the ladder is being built in
        the same wave, so this tolerates both shapes rather than hard-coupling
        to one that may not exist yet. 0 means "never escalated".
        """
        ladder = session.get("ladder")
        if isinstance(ladder, dict):
            history = ladder.get("history")
            if isinstance(history, list) and history:
                return len(history)
            rung = ladder.get("rung")
            if isinstance(rung, int):
                return rung
        rung = session.get("ladder_rung")
        if isinstance(rung, int):
            return rung
        reroute = session.get("reroute")
        if isinstance(reroute, dict):
            history = reroute.get("history")
            if isinstance(history, list):
                return len(history)
        return 0

    async def _first_diff_at(self, session_id: str) -> Optional[datetime]:
        """When the session first produced work, from the guard's checkpoints.

        The checkpoint stream is the only per-minute record of progress; without
        it "time to first diff" can only be measured at session end, which is
        the metric that would have caught a stalled agent early."""
        checkpoints = await self._find("guard_checkpoints", {"session_id": session_id})
        stamps = [
            _aware(c.get("at")) for c in checkpoints
            if int(c.get("files") or 0) > 0 or int(c.get("deletions") or 0) > 0
        ]
        stamps = [s for s in stamps if s is not None]
        return min(stamps) if stamps else None

    async def _attribute_usage(self, session: dict) -> dict:
        """Tokens and dollars for the session.

        Order: pi's own transcript (the only per-turn record for a local coding
        agent) → `db.usage` rows already attributed to this session. Unknown
        stays **None**, never 0: a zero would be indistinguishable from a
        measured zero and would quietly deflate "$ per merged change".
        """
        session_id = str(session.get("_id"))
        model = session.get("model")
        backend = session.get("llm") or session.get("backend")

        totals = None
        if (session.get("backend") or "") == "pi-code":
            try:
                totals = await pi_usage(
                    session_id,
                    workspace=session.get("workspace") or session.get("source_repo"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("pi usage lookup failed for %s: %s", session_id, exc)

        if totals:
            model = totals.get("model") or model
            backend = totals.get("backend") or backend
            recorded = await self._record_pi_usage(session_id, totals, model, backend)
            return {
                "tokens_in": totals["tokens_in"],
                "tokens_out": totals["tokens_out"],
                "cost_usd": round(
                    cost_for(model, totals["tokens_in"], totals["tokens_out"], backend), 6
                ),
                "source": totals.get("source", "pi_transcript") + ("+usage" if recorded else ""),
            }

        rows = await self._find("usage", {"session_id": session_id})
        if rows:
            tokens_in = sum(int(r.get("input_tokens") or 0) for r in rows)
            tokens_out = sum(int(r.get("output_tokens") or 0) for r in rows)
            cost = sum(
                cost_for(
                    r.get("model"), int(r.get("input_tokens") or 0),
                    int(r.get("output_tokens") or 0), r.get("backend"),
                )
                for r in rows
            )
            return {
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": round(cost, 6),
                "source": "db.usage",
            }

        return {"tokens_in": None, "tokens_out": None, "cost_usd": None, "source": "unavailable"}

    async def _record_pi_usage(
        self, session_id: str, totals: dict, model: Optional[str], backend: Optional[str]
    ) -> bool:
        """Mirror the transcript's totals into `db.usage`, once.

        `db.usage` is where every cost surface reads from (`/usage/cost`, the
        spend circuit breaker); a local session that only ever existed in a JSONL
        under ~/.pi is invisible to all of them. Guarded by a lookup rather than
        an upsert key so a re-score never double-counts.
        """
        try:
            existing = await self.db.usage.find_one(
                {"session_id": session_id, "source": "coding:pi"}
            )
            if existing:
                return False
            from aria.db.usage import UsageRepo

            await UsageRepo(self.db).record(
                model=model or "unknown",
                source="coding:pi",
                input_tokens=int(totals.get("tokens_in") or 0),
                output_tokens=int(totals.get("tokens_out") or 0),
                cache_read_tokens=int(totals.get("cache_read") or 0),
                cache_write_tokens=int(totals.get("cache_write") or 0),
                session_id=session_id,
                backend=backend,
                metadata={"attributed_by": "outcome_scorer", "turns": totals.get("turns")},
            )
            return True
        except Exception as exc:  # noqa: BLE001 — accounting must not break scoring
            logger.debug("could not mirror pi usage for %s: %s", session_id, exc)
            return False

    async def _project_slug(self, session: dict) -> Optional[str]:
        """Attribute the session to a project by the ONE rule this codebase has.

        `PathIndex` is most-specific-root-wins; plain prefix matching would file
        a `.worktrees/*` session under whatever coarse row owns the parent
        directory, which is how attribution ends up somewhere nobody looks.
        """
        try:
            from aria.api.routes.digest import PathIndex

            docs = await self.db.projects.find({}).to_list(length=500)
            slug = PathIndex.from_docs(docs, value="slug").session_owner(session)
            if slug:
                return slug
        except Exception as exc:  # noqa: BLE001
            logger.debug("project attribution failed: %s", exc)
        path = session.get("source_repo") or session.get("workspace")
        return os.path.basename((path or "").rstrip("/")) or None

    async def _charter(self, project_slug: Optional[str]) -> Optional[dict]:
        if not project_slug:
            return None
        doc = await self._find_one("projects", {"slug": project_slug})
        charter = (doc or {}).get("charter")
        return charter if isinstance(charter, dict) else None

    # -- persistence ----------------------------------------------------

    async def _store(self, outcome: dict) -> None:
        try:
            await getattr(self.db, OUTCOMES_COLLECTION).update_one(
                {"session_id": outcome["session_id"]}, {"$set": outcome}, upsert=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "could not persist outcome for %s: %s", outcome.get("session_id"), exc
            )

    async def _has_outcome(self, session_id: str) -> bool:
        doc = await self._find_one(OUTCOMES_COLLECTION, {"session_id": session_id})
        return doc is not None

    async def _find_one(self, collection: str, flt: dict) -> Optional[dict]:
        try:
            return await getattr(self.db, collection).find_one(flt)
        except Exception as exc:  # noqa: BLE001 — a missing collection is not an error
            logger.debug("read %s failed: %s", collection, exc)
            return None

    async def _find(self, collection: str, flt: dict, limit: int = 500) -> list[dict]:
        try:
            return await getattr(self.db, collection).find(flt).to_list(length=limit)
        except Exception as exc:  # noqa: BLE001
            logger.debug("read %s failed: %s", collection, exc)
            return []

    async def _latest(self, collection: str, flt: dict, field: str) -> Optional[dict]:
        docs = await self._find(collection, flt)
        if not docs:
            return None
        return sorted(docs, key=lambda d: _aware(d.get(field)) or datetime.min.replace(
            tzinfo=timezone.utc
        ))[-1]


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class OutcomeWorker:
    """Scores terminal sessions on a timer (shape: shells/selfcheck.py).

    A worker rather than a hook in `session.py` on purpose: every finalize path
    (subprocess exit, shell-substrate poll, stop_session, the reaper) would
    otherwise need its own call, and the one that got missed would be the one
    that mattered. Polling also backfills sessions that ended before this
    existed. It runs no LLM — the different-family review is invoked
    deliberately, never on a timer.
    """

    def __init__(self, db, notifier=None, interval_seconds: int = 300, batch: int = 25):
        self.db = db
        self.scorer = OutcomeScorer(db, notifier)
        self.interval = max(30, int(interval_seconds))
        self.batch = max(1, int(batch))
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        if not settings.outcome_scoring_enabled:
            logger.info("outcome scorer disabled (outcome_scoring_enabled=False)")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="steward.outcomes")
        logger.info("outcome scorer started (every %ds, batch %d)", self.interval, self.batch)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def tick(self) -> list[dict]:
        return await self.scorer.score_pending(limit=self.batch)

    async def _run(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=60)  # settle on boot
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                scored = await self.tick()
                if scored:
                    logger.info("scored %d coding session outcome(s)", len(scored))
            except Exception as exc:  # noqa: BLE001
                logger.warning("outcome tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue


# ---------------------------------------------------------------------------
# Metrics — what the weekly report reads
# ---------------------------------------------------------------------------

def _rate(numerator: int, denominator: int) -> Optional[float]:
    """None, not 0.0, when there is nothing to divide. A rate of 0% and 'no data
    yet' are different claims, and the first one would read as a regression."""
    return round(numerator / denominator, 4) if denominator else None


def _bucket(rows: list[dict], key: str) -> dict:
    """success rate + counts grouped by one field."""
    out: dict[str, dict] = {}
    for row in rows:
        name = str(row.get(key) or "unknown")
        slot = out.setdefault(name, {"sessions": 0, "verified": 0, "success": 0})
        slot["sessions"] += 1
        if row.get("verified"):
            slot["verified"] += 1
            if row.get("success"):
                slot["success"] += 1
    for slot in out.values():
        slot["success_rate"] = _rate(slot["success"], slot["verified"])
    return out


async def metrics(
    db,
    *,
    days: int = 7,
    project_slug: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """The metric set the weekly report is built from (proposal §8).

    Computed in Python over a bounded window rather than as an aggregation
    pipeline: the window is hundreds of rows on this box, all four collections
    need joining on different keys anyway, and keeping it as plain reads means
    the report can be exercised in a test without a live Mongo.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=max(1, int(days)))

    async def read(collection: str, flt: dict) -> list[dict]:
        try:
            return await getattr(db, collection).find(flt).to_list(length=5000)
        except Exception as exc:  # noqa: BLE001
            logger.debug("metrics read %s failed: %s", collection, exc)
            return []

    outcomes = [
        row for row in await read(OUTCOMES_COLLECTION, {})
        if (_aware(row.get("created_at")) or now) >= since
        and (project_slug is None or row.get("project_slug") == project_slug)
    ]
    verified = [r for r in outcomes if r.get("verified")]
    successes = [r for r in verified if r.get("success")]
    merged = [r for r in outcomes if r.get("merged")]

    # 2. gate pass rate — over sessions where a gate actually ran.
    gated = [r for r in outcomes if r.get("gate_passed") is not None]
    gate_pass = [r for r in gated if r.get("gate_passed")]

    # 3. nudges per success — the cost, in interventions, of each good session.
    nudges_total = sum(int(r.get("nudges") or 0) for r in outcomes)
    nudges_per_success = (
        round(nudges_total / len(successes), 2) if successes else None
    )

    # 4. stall rate — a session that needed the ladder at all.
    stalled = [r for r in outcomes if int(r.get("rungs_used") or 0) > 0]
    rung_distribution: dict[str, int] = {}
    for row in outcomes:
        rung_distribution[str(int(row.get("rungs_used") or 0))] = (
            rung_distribution.get(str(int(row.get("rungs_used") or 0)), 0) + 1
        )

    # 5. time to first diff.
    ttfd = [
        int(r["time_to_first_diff_seconds"]) for r in outcomes
        if isinstance(r.get("time_to_first_diff_seconds"), int)
    ]
    ttfd_sorted = sorted(ttfd)

    # 6. raises/day + false-raise rate. A raise is an alert that asked for Ben;
    # a false raise is one he answered IGNORE — the tuning signal for the ladder.
    alerts = [
        row for row in await read("alerts", {"needs_human": True})
        if (_aware(row.get("created_at")) or now) >= since
        and (project_slug is None or row.get("project_slug") == project_slug)
    ]
    ignored = [
        a for a in alerts
        if str(((a.get("decision") or {}).get("value") or "")).upper() in ("IGNORE", "REJECT")
    ]

    # 7. guard safety counters. Target is zero; anything else is the headline.
    guard_events = [
        row for row in await read("guard_events", {})
        if (_aware(row.get("at")) or now) >= since
    ]
    blocked = [e for e in guard_events if e.get("blocked")]
    tamper = [e for e in guard_events if str(e.get("kind") or "").startswith("policy:tamper")]

    # 8. tokens and dollars per merged change — the number that says whether the
    # whole apparatus is worth running.
    tokens_all = sum(
        int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0) for r in outcomes
    )
    cost_all = sum(float(r.get("cost_usd") or 0.0) for r in outcomes)
    attributed = [r for r in outcomes if r.get("tokens_in") is not None]

    return {
        "window_days": int(days),
        "since": since,
        "project_slug": project_slug,
        "sessions": len(outcomes),
        "verified_sessions": len(verified),
        "unverified_sessions": len(outcomes) - len(verified),
        "success_rate": _rate(len(successes), len(verified)),
        "success_rate_by_model": _bucket(outcomes, "model"),
        "success_rate_by_tier": _bucket(outcomes, "tier"),
        "success_rate_by_project": _bucket(outcomes, "project_slug"),
        "gate_runs": len(gated),
        "gate_pass_rate": _rate(len(gate_pass), len(gated)),
        "nudges_total": nudges_total,
        "nudges_per_success": nudges_per_success,
        "stall_rate": _rate(len(stalled), len(outcomes)),
        "rung_distribution": rung_distribution,
        "time_to_first_diff_seconds": {
            "count": len(ttfd_sorted),
            "median": ttfd_sorted[len(ttfd_sorted) // 2] if ttfd_sorted else None,
            "p90": ttfd_sorted[int(len(ttfd_sorted) * 0.9)] if ttfd_sorted else None,
        },
        "raises": len(alerts),
        "raises_per_day": round(len(alerts) / max(1, int(days)), 2),
        "false_raises": len(ignored),
        "false_raise_rate": _rate(len(ignored), len(alerts)),
        "rollbacks": sum(1 for r in outcomes if r.get("rolled_back")),
        "blocked_actions": len(blocked),
        "tamper_events": len(tamper),
        "merged_changes": len(merged),
        "tokens_total": tokens_all,
        "cost_usd_total": round(cost_all, 4),
        "tokens_per_merged_change": (
            round(tokens_all / len(merged), 1) if merged else None
        ),
        "cost_usd_per_merged_change": (
            round(cost_all / len(merged), 4) if merged else None
        ),
        # Coverage, so a flattering cost number can't hide behind missing data.
        "usage_attribution_rate": _rate(len(attributed), len(outcomes)),
    }
