"""
ARIA - Meta Supervisor

Purpose: watch every running agent for the stuck signals a pane hash cannot
see, and push a stuck agent forward through a bounded escalation ladder —
raising to Ben only when the ladder is exhausted, never for a stall it fixed.

What was here before: `agents/watchdog.py` detected exactly ONE thing (the md5
of the last 100 pane lines unchanged for 60 s), labelled it with a regex, and
notified. No action, no ladder — and the notification was dropped by the old
`coding:*` filter, so in practice nothing at all happened. Meanwhile pi writes a
structured transcript with every tool call and per-turn usage in it, which ARIA
had never opened (see `pi_transcript.py`).

Division of labour, kept deliberately: the watchdog OBSERVES (it already holds
fresh pane text on every tick and publishes pane-derived signals via
`signal_snapshot()`); this worker DECIDES. Anything that stops, restarts,
re-routes or parks a session needs the project's charter, its budget and the
guard, none of which belong in a pane monitor.

Signals (proposal §6.1 — thresholds are `settings.meta_*`, whose defaults come
from OpenHands' stuck detector, the only published tested values for this):

  no-diff progress   worktree fingerprint unchanged across N nudges
  repeated error     the same error line >= N times (pane and/or transcript)
  tool loop          same (tool, args) >= N; A-B-A-B alternation >= N
  monologue          >= N assistant turns with no tool call
  nudge echo         the same reply to two consecutive nudges
  crash-as-completed a session that exited in < N s with nothing to show
  budget             wall clock / tokens against the project's effective budget

  EXEMPTION          a real long-running child process in the worktree. This is
                     OpenHands issue #5355 exactly, and it is the one failure
                     mode that makes a supervisor worse than none: killing a
                     legitimate 20-minute test run because the pane went quiet.

Ladder (proposal §6.2). Every rung writes an `alerts` row (severity=info,
needs_human=False), a `guard_events` row, and a `meta_escalations` row:

  L0 log -> L1 targeted nudge naming the SPECIFIC signal (<=3, each must show
  diff progress) -> L2 fresh-context restart from the last guard checkpoint
  with a Reflexion note -> L3 one re-route to a stronger tier if the charter's
  `tiers_allowed` permits -> L4 decompose proposal -> L5 park: stop, park the
  branch through the guard, postmortem, needs_human=True.

`settings.meta_ladder_max_rung` caps how far it may climb autonomously; the
rung after the cap is always L5 park, because "we ran out of moves" must end at
Ben rather than in silence. CIRCUIT BREAKER: more than
`settings.meta_raises_per_project_per_day` raises on one project in 24 h
proposes pausing that project and raises ONCE — escalation fatigue is itself a
failure mode (principle 13).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from aria.config import settings
from aria.guard import policy as guard_policy
from aria.steward import pi_transcript

logger = logging.getLogger(__name__)

ESCALATIONS_COLLECTION = "meta_escalations"

# Ladder rungs.
L0_LOG = 0
L1_NUDGE = 1
L2_RESTART = 2
L3_REROUTE = 3
L4_DECOMPOSE = 4
L5_PARK = 5
RUNG_NAMES = {
    L0_LOG: "log",
    L1_NUDGE: "nudge",
    L2_RESTART: "restart",
    L3_REROUTE: "reroute",
    L4_DECOMPOSE: "decompose",
    L5_PARK: "park",
}
# "<= 3 nudges, each must show diff progress" (§6.2). Not a setting: it is the
# shape of the rung, not a tuning knob, and `meta_ladder_max_rung` is the knob
# that decides how much autonomy the ladder gets.
MAX_L1_NUDGES = 3

# Model tiers as the charter spells them (`Charter.tiers_allowed`:
# local|ridge|red|cloud), weakest first. L3 climbs exactly one step.
TIER_LADDER = ("local", "ridge", "red", "cloud")

# Cross-kind liveness thresholds (§6.1). Constants rather than settings: they
# are "this thing has obviously stopped" markers, not tuning surface, and the
# plan fixes their values.
EXTRACTION_STALE_HOURS = 6
DREAM_STALE_HOURS = 48
RELAY_UNDELIVERED_MINUTES = 20
RESEARCH_EXPECTED_MINUTES = 30
RESEARCH_STALL_MULTIPLIER = 2
RELAY_DEDUP_KEY = "meta:relay:undelivered"

# Only correct a crash-as-completed inside this window. Without it, enabling the
# worker would rewrite the status of every session in history on its first tick.
CRASH_LOOKBACK_MINUTES = 60

# Processes that ARE the agent (or its shell), so their mere presence in the
# worktree proves nothing about progress. Everything else running there —
# pytest, make, cargo, npm, docker — is real work and earns the exemption.
AGENT_COMMS = frozenset({
    "pi", "node", "claude", "codex", "deno", "bun",
    "bash", "sh", "dash", "zsh", "fish", "tmux", "login", "su",
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass
class Signal:
    """One reason to believe an agent is stuck."""
    name: str
    detail: str
    source: str = "meta"  # pane | transcript | git | budget | process

    def to_dict(self) -> dict:
        return {"name": self.name, "detail": self.detail, "source": self.source}


# ---------------------------------------------------------------------------
# Long-running-child exemption (OpenHands #5355)
# ---------------------------------------------------------------------------

def long_running_children(
    workspace: str,
    min_seconds: float,
    *,
    proc_root: str = "/proc",
    boot_time: Optional[float] = None,
    now: Optional[float] = None,
) -> list[dict]:
    """Processes working inside `workspace` for longer than `min_seconds`.

    Read straight from /proc (no psutil — no new dependencies): a candidate is
    any process whose cwd is the worktree or below it, whose comm is not the
    agent or a shell, and which has been alive longer than the stall window.

    Returning a non-empty list means "this session is waiting on something real"
    and the ladder stands down. The check is deliberately biased toward
    exempting: a false exemption costs one wasted supervisor tick, while a false
    escalation kills a legitimate long build — the failure that makes a
    supervisor worse than none.
    """
    root = os.path.realpath(os.path.expanduser(workspace or ""))
    if not root or root == "/":
        return []
    clock = now if now is not None else time.time()
    if boot_time is None:
        boot_time = _boot_time(proc_root)
    if boot_time is None:
        return []
    try:
        hz = os.sysconf("SC_CLK_TCK") or 100
    except (ValueError, OSError):  # pragma: no cover - every Linux has it
        hz = 100

    found: list[dict] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        base = os.path.join(proc_root, entry)
        try:
            cwd = os.path.realpath(os.path.join(base, "cwd"))
        except OSError:
            continue
        if cwd != root and not cwd.startswith(root + os.sep):
            continue
        try:
            with open(os.path.join(base, "stat"), "r", encoding="utf-8", errors="replace") as fh:
                stat_line = fh.read()
        except OSError:
            continue
        comm, start_ticks, cpu_ticks = _parse_stat(stat_line)
        if start_ticks is None:
            continue
        elapsed = clock - (boot_time + start_ticks / hz)
        if elapsed < min_seconds:
            continue
        if comm.lower() in AGENT_COMMS:
            continue
        found.append({
            "pid": int(entry),
            "comm": comm,
            "elapsed_seconds": round(elapsed, 1),
            "cpu_ticks": cpu_ticks,
            "cwd": cwd,
        })
    return found


def _parse_stat(stat_line: str) -> tuple[str, Optional[int], int]:
    """(comm, starttime_ticks, utime+stime_ticks) from /proc/<pid>/stat.

    comm is parsed by rfind(')') because a process name may itself contain
    spaces and parentheses — splitting on whitespace shifts every later field
    and silently reads garbage as the start time.
    """
    close = stat_line.rfind(")")
    open_paren = stat_line.find("(")
    if close < 0 or open_paren < 0 or close < open_paren:
        return ("", None, 0)
    comm = stat_line[open_paren + 1:close]
    fields = stat_line[close + 2:].split()
    # fields[0] is field 3 (state), so field 22 (starttime) is index 19 and
    # fields 14/15 (utime/stime) are indexes 11/12.
    try:
        starttime = int(fields[19])
        cpu = int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return (comm, None, 0)
    return (comm, starttime, cpu)


def _boot_time(proc_root: str) -> Optional[float]:
    try:
        with open(os.path.join(proc_root, "stat"), "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

def _new_state() -> dict:
    return {
        "rung": L0_LOG,
        "nudges": 0,
        "fingerprint": None,
        "fingerprint_changed_at": None,
        "nudges_at_fingerprint": 0,
        "last_action_at": None,
        "history": [],
        "parent_session_id": None,
        "exempt_until": None,
    }


class MetaSupervisor:
    """Stuck detection + the escalation ladder, across every agent kind.

    Follows the start()/stop() worker shape of `shells/selfcheck.py` and is OFF
    by default (`settings.meta_supervisor_enabled`).
    """

    def __init__(
        self,
        db,
        session_manager=None,
        notification_service=None,
        *,
        watchdog=None,
        planning_service=None,
        guard=None,
        interval_seconds: Optional[int] = None,
    ):
        self.db = db
        self.session_manager = session_manager
        self.notifier = notification_service
        self.watchdog = watchdog
        self.planning_service = planning_service
        self._guard = guard
        self.interval = max(5, int(
            interval_seconds
            if interval_seconds is not None
            else settings.meta_supervisor_interval_seconds
        ))
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._state: dict[str, dict] = {}
        self._last_tick: dict = {}

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="steward.meta_supervisor")
        logger.info("meta supervisor started (every %ds, ladder cap L%d)",
                    self.interval, settings.meta_ladder_max_rung)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None
        logger.info("meta supervisor stopped")

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "interval_seconds": self.interval,
            "tracked_sessions": len(self._state),
            "ladder_max_rung": settings.meta_ladder_max_rung,
            "last_tick": self._last_tick,
        }

    async def _run(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=30)  # settle on boot
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.evaluate_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad tick must not end the loop
                logger.error("meta supervisor tick failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ tick

    async def evaluate_once(self) -> dict:
        """One full pass. Separated from the loop so it can be unit-tested."""
        result: dict = {"at": _now(), "sessions": 0, "escalations": [], "liveness": []}

        sessions = await self._running_sessions()
        result["sessions"] = len(sessions)
        pane_signals = {}
        if self.watchdog is not None:
            try:
                pane_signals = self.watchdog.signal_snapshot()
            except Exception as exc:  # noqa: BLE001
                logger.debug("watchdog signal snapshot unavailable: %s", exc)

        seen: set[str] = set()
        for session in sessions:
            session_id = str(session.get("_id") or "")
            if not session_id:
                continue
            seen.add(session_id)
            try:
                signals, exemption = await self.collect_signals(
                    session, pane_signals.get(session_id) or {}
                )
                if exemption is not None:
                    result["escalations"].append(
                        {"session_id": session_id, "action": "exempt", "detail": exemption}
                    )
                    continue
                if signals:
                    outcome = await self.escalate(session, signals)
                    if outcome:
                        result["escalations"].append(outcome)
            except Exception as exc:  # noqa: BLE001 — one session must not blind the rest
                logger.error("meta supervisor failed on session %s: %s",
                             session_id, exc, exc_info=True)

        # Drop state for sessions that are no longer running, EXCEPT ones whose
        # ladder state was handed to a restart/re-route child this tick.
        for stale in set(self._state) - seen:
            if self._state[stale].get("handed_off"):
                continue
            self._state.pop(stale, None)

        result["crashes"] = await self.check_crash_as_completed()
        if settings.meta_worker_liveness_enabled:
            result["liveness"] = await self.check_worker_liveness()

        self._last_tick = {
            "at": result["at"],
            "sessions": result["sessions"],
            "escalations": len(result["escalations"]),
            "liveness_findings": len(result["liveness"]),
        }
        return result

    async def _running_sessions(self) -> list[dict]:
        if self.session_manager is None:
            return []
        try:
            return await self.session_manager.list_sessions(status="running") or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("meta supervisor could not list sessions: %s", exc)
            return []

    # --------------------------------------------------------------- signals

    async def collect_signals(
        self, session: dict, pane: Optional[dict] = None
    ) -> tuple[list[Signal], Optional[str]]:
        """Signals for one running session, plus an exemption reason if any.

        Returns ([], reason) when the session is waiting on real work — the
        caller must not escalate in that case, and must not treat the empty
        signal list as "healthy" either.
        """
        session_id = str(session["_id"])
        pane = pane or {}
        state = self._state.setdefault(session_id, _new_state())
        workspace = session.get("workspace") or ""

        # Progress fingerprint FIRST: it resets the nudge accounting, and it is
        # also the thing that says a nudge worked.
        fingerprint = await self._progress_fingerprint(session_id, workspace)
        nudges = self._total_nudges(session, state)
        if fingerprint != state.get("fingerprint"):
            state["fingerprint"] = fingerprint
            state["fingerprint_changed_at"] = _now()
            state["nudges_at_fingerprint"] = nudges

        signals: list[Signal] = []

        nudges_without_progress = nudges - int(state.get("nudges_at_fingerprint") or 0)
        if nudges and nudges_without_progress >= settings.meta_no_diff_nudges:
            signals.append(Signal(
                "no_diff",
                f"{nudges_without_progress} nudges with no change to the worktree",
                "git",
            ))

        if pane.get("repeated_error"):
            err = pane["repeated_error"]
            signals.append(Signal(
                "repeated_error",
                f"the line {err.get('line', '')!r} repeats {err.get('count')}x in the output",
                "pane",
            ))
        if pane.get("nudge_echo"):
            signals.append(Signal(
                "nudge_echo",
                "the agent replied identically to two consecutive nudges",
                "pane",
            ))

        signals.extend(await self._transcript_signals(session))
        signals.extend(await self._budget_signals(session, state))

        if not signals:
            return ([], None)

        # Only pay for the /proc scan when we were about to act.
        children = await asyncio.to_thread(
            long_running_children, workspace, float(settings.coding_stall_seconds)
        )
        if children:
            first = children[0]
            reason = (
                f"waiting on {first['comm']} (pid {first['pid']}, "
                f"{first['elapsed_seconds']:.0f}s) in the worktree"
            )
            state["exempt_until"] = _now()
            logger.info("meta supervisor: %s exempt — %s", session_id, reason)
            return ([], reason)

        return (signals, None)

    def _total_nudges(self, session: dict, state: dict) -> int:
        """Ralph-loop nudges plus the ladder's own. Both re-feed the same agent
        through the same send_input, so both have to count against progress."""
        return int(session.get("loop_nudges") or 0) + int(session.get("meta_nudges") or 0)

    async def _progress_fingerprint(self, session_id: str, workspace: str) -> str:
        """What "progress" means for a session.

        `git diff` alone is not enough: the guard commits checkpoints, and each
        commit empties the diff — so a productive session would look frozen at
        exactly the moment it did the most work. The fingerprint therefore
        combines the working-tree diff with the newest guard checkpoint sha.
        """
        diff = ""
        if self.session_manager is not None:
            try:
                diff = await self.session_manager.get_diff(session_id) or ""
            except Exception as exc:  # noqa: BLE001
                logger.debug("diff unavailable for %s: %s", session_id, exc)
        sha = ""
        try:
            doc = await self.db[guard_gitguard_checkpoints()].find_one(
                {"session_id": session_id}, sort=[("at", -1)]
            )
            sha = str((doc or {}).get("sha") or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug("checkpoint lookup failed for %s: %s", session_id, exc)
        return hashlib.md5(f"{sha}|{diff}".encode("utf-8", "replace")).hexdigest()

    async def _transcript_signals(self, session: dict) -> list[Signal]:
        """Tool loop / alternation / monologue / repeated error, from pi's JSONL.

        Silently empty for a backend that writes no transcript — that is a gap
        in coverage, not an error, and pretending otherwise would make every
        claude_code session look stuck.
        """
        session_id = str(session["_id"])
        try:
            transcript = await pi_transcript.load_transcript(
                session_id, session.get("workspace")
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("transcript unreadable for %s: %s", session_id, exc)
            return []
        if transcript is None:
            return []

        signals: list[Signal] = []
        repeat = transcript.repeating_tool_call(settings.meta_tool_loop_threshold)
        if repeat:
            signals.append(Signal(
                "tool_loop",
                f"called {repeat[0]} with identical arguments {repeat[2]}x in a row",
                "transcript",
            ))
        alternating = transcript.alternating_tool_pair(settings.meta_alternating_loop_threshold)
        if alternating:
            signals.append(Signal(
                "alternating_loop",
                f"alternating between {alternating[0]} and {alternating[1]} "
                f"for {alternating[2]} calls",
                "transcript",
            ))
        monologue = transcript.trailing_monologue_turns()
        if monologue >= settings.meta_monologue_threshold:
            signals.append(Signal(
                "monologue",
                f"{monologue} consecutive turns with no tool call",
                "transcript",
            ))
        error = transcript.repeated_error(settings.meta_repeated_error_threshold)
        if error:
            signals.append(Signal(
                "repeated_error",
                f"the transcript records {error[1]} identical errors: {error[0]!r}",
                "transcript",
            ))
        return signals

    async def _budget_signals(self, session: dict, state: dict) -> list[Signal]:
        """Wall clock and tokens against the project's effective budget.

        Nudge count is NOT a separate budget line: `effective_budget` has no
        nudge field, and the ladder already caps nudges at MAX_L1_NUDGES. A
        second, invented number there would just be a knob nobody set.
        """
        budget = (await self._project_context(session))[2]
        signals: list[Signal] = []

        started = _aware(session.get("loop_started_at")) or _aware(session.get("created_at"))
        minutes = budget.get("session_minutes")
        if started and minutes:
            elapsed = (_now() - started).total_seconds() / 60.0
            if elapsed > float(minutes):
                signals.append(Signal(
                    "budget_wall_clock",
                    f"running {elapsed:.0f}m against a {minutes}m session budget",
                    "budget",
                ))

        cap = budget.get("local_tokens_per_day")
        if cap:
            try:
                transcript = await pi_transcript.load_transcript(
                    str(session["_id"]), session.get("workspace")
                )
            except Exception:  # noqa: BLE001
                transcript = None
            # Session total vs the DAILY cap: one session that alone exceeds the
            # whole project's daily allowance is unambiguous, and per-day
            # aggregation belongs to the steward, which owns the budget ledger.
            if transcript is not None and transcript.usage.total > int(cap):
                signals.append(Signal(
                    "budget_tokens",
                    f"{transcript.usage.total} tokens against a {cap}/day budget",
                    "budget",
                ))
        return signals

    # ------------------------------------------------------------ the ladder

    async def escalate(self, session: dict, signals: list[Signal]) -> Optional[dict]:
        """Climb one rung. Returns the recorded escalation, or None if it was
        too soon to act again."""
        session_id = str(session["_id"])
        state = self._state.setdefault(session_id, _new_state())
        now = _now()

        # Debounce: a rung gets at least one stall window to work before the
        # next one fires. Without this, a 30 s tick would run the whole ladder
        # in three minutes and park a session that was about to answer.
        last = _aware(state.get("last_action_at"))
        if last and (now - last).total_seconds() < settings.coding_stall_seconds:
            return None

        rung = int(state.get("rung") or L0_LOG)
        max_rung = int(settings.meta_ladder_max_rung)
        if rung > max_rung and rung < L5_PARK:
            # Out of sanctioned moves: park rather than keep nudging. Silence is
            # not an option once the ladder is exhausted.
            rung = L5_PARK

        detail = "; ".join(s.detail for s in signals)
        state["last_action_at"] = now
        outcome: dict

        if rung == L0_LOG:
            outcome = {"action": "log", "ok": True, "detail": detail}
            state["rung"] = L1_NUDGE
        elif rung == L1_NUDGE:
            outcome = await self._rung_nudge(session, state, signals)
            if not outcome.get("ok"):
                state["rung"] = L2_RESTART
        elif rung == L2_RESTART:
            outcome = await self._rung_restart(session, state, signals)
            state["rung"] = L3_REROUTE
        elif rung == L3_REROUTE:
            outcome = await self._rung_reroute(session, state, signals)
            state["rung"] = L4_DECOMPOSE
        elif rung == L4_DECOMPOSE:
            outcome = await self._rung_decompose(session, state, signals)
            state["rung"] = L5_PARK
        else:
            outcome = await self._rung_park(session, state, signals)
            state["rung"] = L5_PARK
            state["terminal"] = True

        record = await self._record(session, rung, signals, outcome)
        state.setdefault("history", []).append(
            {"rung": rung, "at": now, "action": outcome.get("action")}
        )
        return record

    # -- L1 ----------------------------------------------------------------

    async def _rung_nudge(self, session: dict, state: dict, signals: list[Signal]) -> dict:
        """A nudge that NAMES the signal. A generic "keep going" is what the
        Ralph loop already sends; repeating it is why the agent echoes."""
        session_id = str(session["_id"])
        if int(state.get("nudges") or 0) >= MAX_L1_NUDGES:
            return {"action": "nudge", "ok": False, "reason": f"{MAX_L1_NUDGES} nudges spent"}
        # "each must show diff progress" (§6.2): the no_diff signal IS the
        # report that the previous nudge changed nothing, so spending another
        # one on it is the echo loop the rung exists to escape.
        if int(state.get("nudges") or 0) and any(s.name == "no_diff" for s in signals):
            return {"action": "nudge", "ok": False,
                    "reason": "the last nudge produced no change to the worktree"}
        if self.session_manager is None:
            return {"action": "nudge", "ok": False, "reason": "no session manager"}

        text = self._nudge_text(signals)
        try:
            await self.session_manager.send_input(session_id, text)
        except Exception as exc:  # noqa: BLE001
            return {"action": "nudge", "ok": False, "reason": f"send_input failed: {exc}"}

        state["nudges"] = int(state.get("nudges") or 0) + 1
        # `meta_nudges` is a separate counter from `loop_nudges` on purpose: a
        # Ralph loop ends at `max_nudges`, and letting the supervisor consume
        # that budget would silently shorten a healthy loop's life.
        try:
            await self.db.coding_sessions.update_one(
                {"_id": session_id},
                {"$inc": {"meta_nudges": 1},
                 "$set": {"meta_last_nudge_at": _now(), "updated_at": _now()}},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not record meta nudge for %s: %s", session_id, exc)
        return {"action": "nudge", "ok": True, "nudge": state["nudges"], "text": text}

    @staticmethod
    def _nudge_text(signals: list[Signal]) -> str:
        named = "\n".join(f"- {s.name}: {s.detail}" for s in signals)
        return (
            "[ARIA supervisor] You appear to be stuck. Specifically:\n"
            f"{named}\n\n"
            "Do NOT repeat the last action. Either (a) state in one line what is "
            "blocking you and what you need, or (b) take a DIFFERENT concrete "
            "step toward the task and make a change to a file. If the task is "
            "already complete, say so and stop."
        )

    # -- L2 ----------------------------------------------------------------

    async def _rung_restart(self, session: dict, state: dict, signals: list[Signal]) -> dict:
        """Fresh context from the last checkpoint, with a Reflexion-style note.

        The note is written as the checkpoint's `notes` rather than passed to
        `resume_session`, because `build_resume_prompt` already renders notes
        into the resume prompt — so the failure history lands in the new
        agent's first message without touching session.py.
        """
        session_id = str(session["_id"])
        workspace = session.get("workspace") or ""
        if self.session_manager is None or not workspace:
            return {"action": "restart", "ok": False, "reason": "no session manager/workspace"}

        note = self._reflexion_note(session, state, signals)
        try:
            from aria.agents.checkpoint import write_checkpoint

            await write_checkpoint(
                self.db, session_id, workspace,
                current_step="stuck — supervised restart", notes=note,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("meta supervisor could not checkpoint %s: %s", session_id, exc)

        await self._guard_checkpoint(session_id, "pre-restart")
        await self._stop_session(session_id)

        try:
            fresh = await self.session_manager.resume_session(
                workspace=workspace,
                backend=session.get("backend"),
                model=session.get("model"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"action": "restart", "ok": False, "reason": f"resume failed: {exc}"}
        if not fresh:
            return {"action": "restart", "ok": False, "reason": "no resumable checkpoint"}

        new_id = str(fresh.get("_id") or fresh.get("id") or "")
        self._hand_off(session_id, new_id, state, L3_REROUTE)
        return {"action": "restart", "ok": True, "new_session_id": new_id, "note": note}

    def _reflexion_note(self, session: dict, state: dict, signals: list[Signal]) -> str:
        history = ", ".join(
            f"L{item['rung']} {item.get('action')}" for item in state.get("history") or []
        )
        lines = [
            "The last attempt failed. What went wrong, so you do not repeat it:",
            *[f"- {s.name}: {s.detail}" for s in signals],
        ]
        if history:
            lines.append(f"- supervision already tried: {history}")
        lines.append(
            "Start by re-reading the current state of the files rather than "
            "trusting the previous plan, and take a different approach."
        )
        return "\n".join(lines)

    def _hand_off(self, old_id: str, new_id: str, state: dict, rung: int) -> None:
        """Carry the ladder onto a restarted/re-routed child.

        Without this the child starts at L0 and the ladder restarts forever —
        the loop the ladder exists to bound."""
        state["handed_off"] = True
        if not new_id:
            return
        child = _new_state()
        child["rung"] = rung
        child["parent_session_id"] = old_id
        child["history"] = list(state.get("history") or [])
        child["nudges"] = int(state.get("nudges") or 0)
        self._state[new_id] = child

    # -- L3 ----------------------------------------------------------------

    async def _rung_reroute(self, session: dict, state: dict, signals: list[Signal]) -> dict:
        """One step up the tier ladder, if the charter allows that tier."""
        session_id = str(session["_id"])
        project, charter, _budget = await self._project_context(session)
        allowed = [t for t in (charter.get("tiers_allowed") or []) if t in TIER_LADDER]
        current = self._session_tier(session)
        target = self._next_tier(current, allowed)
        if target is None:
            return {
                "action": "reroute", "ok": False,
                "reason": f"charter allows {allowed or 'no'} tiers; current tier is {current}",
            }

        if target == "cloud":
            cooling = await self._cloud_cooling_down()
            if cooling:
                return {"action": "reroute", "ok": False, "reason": f"cloud tier {cooling}"}

        spec = await self._tier_target(target)
        if spec is None:
            return {"action": "reroute", "ok": False, "reason": f"no launch profile for {target}"}
        if self.session_manager is None:
            return {"action": "reroute", "ok": False, "reason": "no session manager"}

        await self._guard_checkpoint(session_id, "pre-reroute")
        await self._stop_session(session_id)

        prompt = (
            f"{self._reflexion_note(session, state, signals)}\n\n"
            f"--- Original task ---\n{session.get('prompt') or ''}"
        )
        try:
            fresh = await self.session_manager.start_session(
                workspace=session.get("workspace") or "",
                backend=spec.get("backend"),
                model=spec.get("model"),
                llm=spec.get("llm"),
                prompt=prompt,
            )
        except Exception as exc:  # noqa: BLE001
            return {"action": "reroute", "ok": False, "reason": f"start failed: {exc}"}

        new_id = str((fresh or {}).get("_id") or "")
        self._hand_off(session_id, new_id, state, L4_DECOMPOSE)
        return {
            "action": "reroute", "ok": True, "from_tier": current, "to_tier": target,
            "new_session_id": new_id, "project": project,
        }

    @staticmethod
    def _session_tier(session: dict) -> str:
        backend = (session.get("backend") or "").lower()
        llm = (session.get("llm") or "").lower()
        if backend in ("claude_code", "claude-code", "codex"):
            return "cloud"
        if "ridge" in llm or "ridge" in (session.get("host") or "").lower():
            return "ridge"
        if llm == "red" or "red" in (session.get("host") or "").lower():
            return "red"
        return "local"

    @staticmethod
    def _next_tier(current: str, allowed: list[str]) -> Optional[str]:
        try:
            index = TIER_LADDER.index(current)
        except ValueError:
            index = 0
        for tier in TIER_LADDER[index + 1:]:
            if tier in allowed:
                return tier
        return None

    async def _cloud_cooling_down(self) -> Optional[str]:
        """Refuse a cloud re-route while the Claude quota is cooling down.

        `agents/routing.py` already tracks this (the watchdog writes it when it
        sees rate-limit text); escalating into a tier that is rate-limited would
        burn the one re-route the ladder gets."""
        try:
            from aria.agents.routing import get_cooldown

            cooldown = await get_cooldown(self.db)
        except Exception as exc:  # noqa: BLE001
            logger.debug("quota cooldown check failed: %s", exc)
            return None
        if cooldown:
            return "is cooling down (quota exhausted)"
        return None

    async def _tier_target(self, tier: str) -> Optional[dict]:
        """Backend/llm/model for a tier, without editing routing.py.

        Cloud reads `coding_routing_model_deep` because an escalation is by
        definition asking for more reasoning than the tier that just failed.
        Ridge/RED read their `db.agents` launch profile, which is where the
        provider mapping actually lives (CLAUDE.md: two provider-mapping layers).
        """
        if tier == "cloud":
            return {
                "backend": "claude_code",
                "model": settings.coding_routing_model_deep,
                "llm": None,
            }
        slug = {"ridge": "pi-coding-ridge", "red": "pi-coding-red"}.get(tier)
        if not slug:
            return None
        try:
            profile = await self.db.agents.find_one({"slug": slug})
        except Exception as exc:  # noqa: BLE001
            logger.debug("agent profile lookup failed for %s: %s", slug, exc)
            return None
        if not profile:
            return None
        llm_conf = profile.get("llm") or {}
        return {
            "backend": "pi-code",
            "model": llm_conf.get("model"),
            "llm": llm_conf.get("backend"),
        }

    # -- L4 ----------------------------------------------------------------

    async def _rung_decompose(self, session: dict, state: dict, signals: list[Signal]) -> dict:
        """Propose a 2–3 way split rather than spawning it.

        Deliberate: spawning more agents off a task that has already failed four
        rungs is the runaway shape principle 13 warns about, and the cloud model
        the plan wants for the decomposition is not reachable from this box
        today (LOCAL-ONLY since 2026-07-26). The proposal is the artifact — the
        steward or Ben turns it into sessions.
        """
        project, _charter, _budget = await self._project_context(session)
        proposal = {
            "kind": "decompose",
            "session_id": str(session["_id"]),
            "project": project,
            "workspace": session.get("workspace"),
            "original_prompt": (session.get("prompt") or "")[:4000],
            "signals": [s.to_dict() for s in signals],
            "attempts": list(state.get("history") or []),
            "suggested": (
                "Split this task into 2-3 sessions that each end in a runnable "
                "check, and run them one at a time."
            ),
        }
        return {"action": "decompose", "ok": True, "proposal": proposal}

    # -- L5 ----------------------------------------------------------------

    async def _rung_park(self, session: dict, state: dict, signals: list[Signal]) -> dict:
        """Stop, park the branch through the guard, write a postmortem, raise.

        The guard holds the pen here too: checkpoint first so nothing is lost,
        then `discard()`, which removes the worktree and renames the branch to
        `parked/<project>/<sid8>` (gitguard.py). A parked branch is the
        postmortem material — deleting it would make park indistinguishable
        from lose.
        """
        session_id = str(session["_id"])
        project, _charter, _budget = await self._project_context(session)

        await self._guard_checkpoint(session_id, "park")
        await self._stop_session(session_id)

        parked_branch = None
        guard = self._get_guard()
        if guard is not None:
            try:
                result = await guard.discard(session_id)
                parked_branch = (result or {}).get("parked_branch")
            except Exception as exc:  # noqa: BLE001
                logger.warning("meta supervisor could not park %s: %s", session_id, exc)

        postmortem = {
            "session_id": session_id,
            "project": project,
            "workspace": session.get("workspace"),
            "backend": session.get("backend"),
            "model": session.get("model"),
            "parked_branch": parked_branch,
            "signals": [s.to_dict() for s in signals],
            "ladder": list(state.get("history") or []),
            "prompt": (session.get("prompt") or "")[:2000],
            "at": _now(),
        }
        try:
            await self.db.coding_sessions.update_one(
                {"_id": session_id},
                {"$set": {"meta_parked": True, "meta_postmortem": postmortem,
                          "updated_at": _now()}},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not persist postmortem for %s: %s", session_id, exc)

        breaker = await self._circuit_breaker(project)
        return {
            "action": "park", "ok": True, "raise": not breaker,
            "parked_branch": parked_branch, "postmortem": postmortem,
            "circuit_breaker": breaker,
        }

    async def _circuit_breaker(self, project: Optional[str]) -> Optional[dict]:
        """More than N raises on one project in 24 h -> propose a pause, raise
        once, and stop raising for that project.

        Counts the supervisor's OWN raises (`meta_escalations.raised`), not the
        whole alerts table: an unrelated alert storm must not silence the
        ladder, and the ladder's raises must not be diluted by them.
        """
        if not project:
            return None
        limit = int(settings.meta_raises_per_project_per_day)
        since = _now() - timedelta(hours=24)
        try:
            count = await self.db[ESCALATIONS_COLLECTION].count_documents(
                {"project": project, "raised": True, "at": {"$gte": since}}
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("raise count failed for %s: %s", project, exc)
            return None
        # `count` is the raises BEFORE this one, so `count >= limit` means this
        # park would be the (limit+1)-th — "more than N raises in 24h" (§6.3).
        if count < limit:
            return None

        reason = f"{count} supervised escalations exhausted the ladder in 24h"
        paused = False
        service = await self._planning()
        if service is not None:
            try:
                paused = await service.propose_pause(project, reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning("pause proposal failed for %s: %s", project, exc)
        await self._notify(
            source="meta-supervisor",
            event_type="circuit_breaker",
            detail=(f"Paused supervision of '{project}': {reason}. "
                    "Further ladder exhaustions on this project will not raise."),
            severity="high",
            needs_human=True,
            kind="meta",
            dedup_key=f"meta:circuit:{project}",
            cooldown_seconds=24 * 3600,
            proposal={"kind": "pause_project", "project": project, "reason": reason},
        )
        return {"project": project, "raises_24h": count, "pause_proposed": paused}

    # ------------------------------------------------------------ recording

    async def _record(
        self, session: dict, rung: int, signals: list[Signal], outcome: dict
    ) -> dict:
        """Every rung: one alerts row, one guard_event, one meta_escalations row."""
        session_id = str(session["_id"])
        project, _charter, _budget = await self._project_context(session)
        raised = bool(rung >= L5_PARK and outcome.get("raise"))
        names = ",".join(s.name for s in signals)
        detail = "; ".join(s.detail for s in signals)
        rung_name = RUNG_NAMES.get(rung, str(rung))

        record = {
            "session_id": session_id,
            "project": project,
            "rung": rung,
            "rung_name": rung_name,
            "action": outcome.get("action"),
            "ok": bool(outcome.get("ok")),
            "reason": outcome.get("reason"),
            "signals": [s.to_dict() for s in signals],
            "detail": detail,
            "raised": raised,
            "at": _now(),
        }
        for key in ("new_session_id", "parked_branch", "proposal", "postmortem",
                    "circuit_breaker", "from_tier", "to_tier"):
            if outcome.get(key) is not None:
                record[key] = outcome[key]
        try:
            await self.db[ESCALATIONS_COLLECTION].insert_one(dict(record))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist escalation for %s: %s", session_id, exc)

        await guard_policy.record_event(
            self.db,
            kind=f"meta:ladder:{rung_name}",
            detail=f"L{rung} {rung_name} on {session_id}: {detail}"[:1000],
            session_id=session_id,
            severity="warning" if raised else "info",
            actor="meta-supervisor",
            extra={"project": project, "signals": names, "ok": bool(outcome.get("ok"))},
        )

        if raised:
            await self._notify(
                source="meta-supervisor",
                event_type="ladder:exhausted",
                detail=(f"Parked '{project or session_id}' after the escalation ladder "
                        f"was exhausted: {detail}"),
                severity="high",
                needs_human=True,
                kind="meta",
                dedup_key=f"meta:park:{session_id}",
                cooldown_seconds=0,
                project_path=session.get("workspace"),
                proposal={"kind": "parked_session", **(outcome.get("postmortem") or {})},
            )
        else:
            await self._notify(
                source=f"coding:{session_id}",
                event_type=f"meta:l{rung}",
                detail=f"L{rung} {rung_name} ({names}): {detail}"[:1000],
                severity="info",
                needs_human=False,
                kind="meta",
                dedup_key=f"meta:{session_id}:l{rung}",
                cooldown_seconds=0,
                project_path=session.get("workspace"),
                proposal=outcome.get("proposal"),
            )
        return record

    async def _notify(self, **kwargs) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.notify(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("meta supervisor alert failed (%s): %s",
                           kwargs.get("event_type"), exc)

    # ------------------------------------------------- crash-as-completed

    async def check_crash_as_completed(self) -> list[dict]:
        """Relabel `completed` sessions that were really crashes.

        `session.py:_watch_shell_session` marks ANY tmux exit as `completed`
        with `exit_code: None`, so pi dying instantly against a dead provider
        port is indistinguishable from success — and every downstream consumer
        (outcomes, the digest, the steward's "did that work?") believes it.
        A session that exited inside the grace window with no diff and no guard
        checkpoint produced nothing; that is a failure, and it is labelled one.
        """
        corrected: list[dict] = []
        grace = int(settings.meta_crash_grace_seconds)
        since = _now() - timedelta(minutes=CRASH_LOOKBACK_MINUTES)
        try:
            docs = await self.db.coding_sessions.find({
                "status": "completed",
                "exit_code": None,
                "meta_crash_checked": {"$ne": True},
                "completed_at": {"$gte": since},
            }).to_list(length=50)
        except Exception as exc:  # noqa: BLE001
            logger.debug("crash sweep query failed: %s", exc)
            return corrected

        for doc in docs or []:
            session_id = str(doc.get("_id") or "")
            if not session_id:
                continue
            verdict = await self._crash_verdict(doc, grace)
            update: dict = {"meta_crash_checked": True, "updated_at": _now()}
            if verdict is not None:
                update.update({
                    "status": "failed",
                    "error": verdict,
                    "meta_crash_corrected": True,
                })
            try:
                await self.db.coding_sessions.update_one({"_id": session_id}, {"$set": update})
            except Exception as exc:  # noqa: BLE001
                logger.debug("could not mark crash check on %s: %s", session_id, exc)
                continue
            if verdict is None:
                continue

            corrected.append({"session_id": session_id, "reason": verdict})
            await guard_policy.record_event(
                self.db,
                kind="meta:crash_as_completed",
                detail=f"{session_id}: {verdict}",
                session_id=session_id,
                severity="warning",
                actor="meta-supervisor",
            )
            await self._notify(
                source=f"coding:{session_id}",
                event_type="meta:crash_as_completed",
                detail=f"Relabelled completed -> failed: {verdict}",
                severity="info",
                needs_human=False,
                kind="meta",
                dedup_key=f"meta:crash:{session_id}",
                cooldown_seconds=0,
                project_path=doc.get("workspace"),
            )
        return corrected

    async def _crash_verdict(self, doc: dict, grace: int) -> Optional[str]:
        """The reason string when this session was a crash, else None."""
        started = _aware(doc.get("loop_started_at")) or _aware(doc.get("created_at"))
        finished = _aware(doc.get("completed_at"))
        if not started or not finished:
            return None
        elapsed = (finished - started).total_seconds()
        if elapsed >= grace or elapsed < 0:
            return None

        session_id = str(doc["_id"])
        try:
            checkpoint = await self.db[guard_gitguard_checkpoints()].find_one(
                {"session_id": session_id}
            )
        except Exception:  # noqa: BLE001
            checkpoint = None
        if checkpoint:
            return None
        if self.session_manager is not None:
            try:
                diff = await self.session_manager.get_diff(session_id) or ""
            except Exception:  # noqa: BLE001
                diff = ""
            if diff.strip():
                return None
        return (
            f"process exited after {elapsed:.0f}s (< {grace}s grace) with no diff "
            "and no checkpoint — crash, not completion"
        )

    # --------------------------------------------------- worker liveness

    async def check_worker_liveness(self) -> list[dict]:
        """Cross-kind liveness (§6.1). Everything here is informational EXCEPT
        the relay one: undelivered alerts mean Ben is blind, which is the exact
        failure class that hid three outages, so it is needs_human."""
        findings: list[dict] = []
        now = _now()

        if getattr(settings, "shells_extraction_enabled", False):
            stale = await self._stale_extraction(now)
            if stale is not None:
                findings.append(stale)

        findings.extend(await self._stalled_research(now))

        if getattr(settings, "dream_enabled", False):
            stale = await self._stale_dream(now)
            if stale is not None:
                findings.append(stale)

        undelivered = await self._undelivered_alerts(now)
        if undelivered is not None:
            findings.append(undelivered)

        for finding in findings:
            await self._notify(
                source="meta-supervisor",
                event_type=finding["event_type"],
                detail=finding["detail"],
                severity=finding["severity"],
                needs_human=finding["needs_human"],
                kind="liveness",
                dedup_key=finding["dedup_key"],
                cooldown_seconds=finding.get("cooldown", 3600),
            )
        return findings

    async def _stale_extraction(self, now: datetime) -> Optional[dict]:
        try:
            doc = await self.db.shell_extraction_state.find_one(
                {}, sort=[("last_run_at", -1)]
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("extraction cursor read failed: %s", exc)
            return None
        last = _aware((doc or {}).get("last_run_at"))
        if last is None:
            return None  # never ran on this box — not a regression to report
        hours = (now - last).total_seconds() / 3600.0
        if hours < EXTRACTION_STALE_HOURS:
            return None
        return {
            "worker": "shell_extraction",
            "event_type": "worker_stale",
            "detail": f"shell extraction cursor has not advanced in {hours:.1f}h",
            "severity": "info", "needs_human": False,
            "dedup_key": "meta:liveness:extraction",
        }

    async def _stalled_research(self, now: datetime) -> list[dict]:
        limit = RESEARCH_EXPECTED_MINUTES * RESEARCH_STALL_MULTIPLIER
        try:
            docs = await self.db.research_runs.find(
                {"status": {"$in": ["running", "pending"]}}
            ).to_list(length=20)
        except Exception as exc:  # noqa: BLE001
            logger.debug("research liveness read failed: %s", exc)
            return []
        out = []
        for doc in docs or []:
            last = _aware(doc.get("updated_at")) or _aware(doc.get("created_at"))
            if last is None:
                continue
            minutes = (now - last).total_seconds() / 60.0
            if minutes < limit:
                continue
            run_id = str(doc.get("_id"))
            out.append({
                "worker": "research",
                "event_type": "worker_stale",
                "detail": (f"research run {run_id} has shown no progress for "
                           f"{minutes:.0f}m (>{limit}m)"),
                "severity": "info", "needs_human": False,
                "dedup_key": f"meta:liveness:research:{run_id}",
            })
        return out

    async def _stale_dream(self, now: datetime) -> Optional[dict]:
        try:
            doc = await self.db.dream_journal.find_one({}, sort=[("created_at", -1)])
        except Exception as exc:  # noqa: BLE001
            logger.debug("dream liveness read failed: %s", exc)
            return None
        last = _aware((doc or {}).get("created_at"))
        if last is None:
            return None
        hours = (now - last).total_seconds() / 3600.0
        if hours < DREAM_STALE_HOURS:
            return None
        return {
            "worker": "dream",
            "event_type": "worker_stale",
            "detail": f"no dream cycle has completed in {hours:.0f}h",
            "severity": "info", "needs_human": False,
            "dedup_key": "meta:liveness:dream",
        }

    async def _undelivered_alerts(self, now: datetime) -> Optional[dict]:
        """Alerts that need Ben and have not been delivered.

        Complements `notifications/relay.py`, which watches the relay's
        HEARTBEAT: a relay that heartbeats but never sends is still a blind Ben.
        Its own alert is excluded from the query so this can never sustain
        itself once everything else has drained.
        """
        cutoff = now - timedelta(minutes=RELAY_UNDELIVERED_MINUTES)
        try:
            docs = await self.db.alerts.find({
                "needs_human": True,
                "acked": False,
                "delivered_at": None,
                "created_at": {"$lt": cutoff},
                "dedup_key": {"$ne": RELAY_DEDUP_KEY},
            }).to_list(length=100)
        except Exception as exc:  # noqa: BLE001
            logger.debug("undelivered alert read failed: %s", exc)
            return None
        if not docs:
            return None
        oldest = min(
            (_aware(d.get("created_at")) or now) for d in docs
        )
        minutes = (now - oldest).total_seconds() / 60.0
        return {
            "worker": "relay",
            "event_type": "relay_undelivered",
            "detail": (f"{len(docs)} alert(s) need a human and none has been delivered; "
                       f"the oldest has waited {minutes:.0f}m"),
            "severity": "high", "needs_human": True,
            "dedup_key": RELAY_DEDUP_KEY,
            "cooldown": RELAY_UNDELIVERED_MINUTES * 60,
        }

    # ------------------------------------------------------------- helpers

    def _get_guard(self):
        if self._guard is not None:
            return self._guard
        try:
            from aria.guard.gitguard import get_git_guard

            self._guard = get_git_guard(self.db)
        except Exception as exc:  # noqa: BLE001
            logger.debug("guard unavailable: %s", exc)
            return None
        return self._guard

    async def _guard_checkpoint(self, session_id: str, reason: str) -> None:
        """Commit whatever the agent has before we stop it. Advisory: a session
        with no guard worktree simply has nothing to checkpoint."""
        guard = self._get_guard()
        if guard is None:
            return
        try:
            await guard.checkpoint(session_id, reason=reason)
        except Exception as exc:  # noqa: BLE001
            logger.debug("guard checkpoint skipped for %s: %s", session_id, exc)

    async def _stop_session(self, session_id: str) -> None:
        if self.session_manager is None:
            return
        try:
            await self.session_manager.stop_session(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("meta supervisor could not stop %s: %s", session_id, exc)

    async def _planning(self):
        if self.planning_service is not None:
            return self.planning_service
        try:
            from aria.planning.service import PlanningService

            self.planning_service = PlanningService(self.db)
        except Exception as exc:  # noqa: BLE001
            logger.debug("planning service unavailable: %s", exc)
            return None
        return self.planning_service

    async def _project_context(self, session: dict) -> tuple[Optional[str], dict, dict]:
        """(project_slug, charter dict, effective budget) for a session.

        Attribution order: the guard's own record (it knows the repo behind a
        `.worktrees/*` path), then PathIndex over db.projects (most-specific
        root wins — never plain prefix matching, C4), then the basename. A
        worktree path is the normal case for a guarded session, and a basename
        guess there would attribute the work to `<project>-<sid8>`.
        """
        session_id = str(session.get("_id") or "")
        cached = self._state.get(session_id, {}).get("_project_ctx")
        if cached is not None:
            return cached

        slug: Optional[str] = None
        guard = self._get_guard()
        if guard is not None:
            try:
                record = await guard.get_session(session_id)
            except Exception:  # noqa: BLE001
                record = None
            slug = (record or {}).get("project") or None

        path = session.get("source_repo") or session.get("workspace") or ""
        if slug is None and path:
            slug = await self._slug_for_path(path)

        charter: dict = {}
        if slug:
            try:
                doc = await self.db.projects.find_one({"slug": slug})
            except Exception:  # noqa: BLE001
                doc = None
            charter = dict((doc or {}).get("charter") or {})

        try:
            from aria.planning.service import effective_budget

            budget = effective_budget(charter)
        except Exception as exc:  # noqa: BLE001
            logger.debug("budget resolution failed for %s: %s", slug, exc)
            budget = {}

        context = (slug, charter, budget)
        if session_id in self._state:
            self._state[session_id]["_project_ctx"] = context
        return context

    async def _slug_for_path(self, path: str) -> Optional[str]:
        try:
            from aria.api.routes.digest import PathIndex

            docs = await self.db.projects.find(
                {}, {"slug": 1, "path": 1, "relevant_paths": 1}
            ).to_list(length=500)
            index = PathIndex.from_docs(docs or [], value="slug")
            owner = index.owner(path)
            if owner:
                return owner
        except Exception as exc:  # noqa: BLE001
            logger.debug("path attribution failed for %s: %s", path, exc)
        return os.path.basename(str(path).rstrip("/")) or None


def guard_gitguard_checkpoints() -> str:
    """The guard's checkpoint collection name, read from the guard rather than
    duplicated here so a rename there cannot silently break progress detection."""
    try:
        from aria.guard.gitguard import GUARD_CHECKPOINTS_COLLECTION

        return GUARD_CHECKPOINTS_COLLECTION
    except Exception:  # noqa: BLE001 - pragma: no cover
        return "guard_checkpoints"
