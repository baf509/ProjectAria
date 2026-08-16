"""Tests for the MetaSupervisor: pi transcript parsing, stuck signals, the
escalation ladder, the circuit breaker and cross-kind liveness.

The invariants under test are incident-derived, not stylistic:
- a pi transcript is parsed from the REAL schema (recorded off corsair's own
  session files), including the half-written last line a live file always has
- a long-running child process EXEMPTS a session (OpenHands #5355: a supervisor
  that kills a legitimate 20-minute test run is worse than no supervisor)
- the ladder climbs one rung at a time and always ENDS somewhere — at the park
  rung with needs_human, never in silence
- more than N raises on one project in 24 h stops raising and proposes a pause
  (escalation fatigue is itself a failure mode)
- a session that exits in under the grace window with nothing to show is
  FAILED, not completed (session.py marks any tmux exit "completed", so a pi
  crash on a dead provider port reads as success today)
- undelivered needs_human alerts are themselves needs_human: that is Ben being
  blind, the failure class that hid three relay outages
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.agents import watchdog as watchdog_mod
from aria.agents.watchdog import CodingWatchdog, repeated_error_line, reply_hash
from aria.config import settings
from aria.notifications import signal_rpc
from aria.steward import pi_transcript as pt
from aria.steward.supervisor import (
    L0_LOG,
    L1_NUDGE,
    L2_RESTART,
    L3_REROUTE,
    L4_DECOMPOSE,
    L5_PARK,
    MAX_L1_NUDGES,
    MetaSupervisor,
    Signal,
    long_running_children,
)


def _utc(offset_seconds: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


# ---------------------------------------------------------------------------
# Minimal in-memory Mongo stand-in (no mongomock in this venv), same shape as
# tests/test_alerts_v2.py plus the operators this module actually issues.
# ---------------------------------------------------------------------------

def _match(doc: dict, flt: dict) -> bool:
    for key, expected in (flt or {}).items():
        actual = doc.get(key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if op == "$ne":
                    if actual == operand:
                        return False
                elif op == "$in":
                    if actual not in operand:
                        return False
                elif op == "$gte":
                    if actual is None or actual < operand:
                        return False
                elif op == "$lt":
                    if actual is None or actual >= operand:
                        return False
                elif op == "$regex":
                    if actual is None or not re.search(operand, str(actual)):
                        return False
                else:  # pragma: no cover - unsupported operator in a test
                    raise NotImplementedError(op)
        elif actual != expected:
            return False
    return True


def _apply(doc: dict, update: dict) -> None:
    for op, fields in update.items():
        if op == "$set":
            doc.update(fields)
        elif op == "$inc":
            for field, delta in fields.items():
                doc[field] = (doc.get(field) or 0) + delta
        else:  # pragma: no cover
            raise NotImplementedError(op)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction=1):
        self._docs.sort(key=lambda d: (d.get(field) is None, d.get(field)), reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self._docs[:length] if length else self._docs)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = list(docs or [])

    async def insert_one(self, doc):
        doc.setdefault("_id", f"oid-{len(self.docs)}")
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def find_one(self, flt=None, projection=None, *, sort=None, **kwargs):
        candidates = [d for d in self.docs if _match(d, flt or {})]
        if sort:
            field, direction = sort[0]
            candidates.sort(
                key=lambda d: (d.get(field) is None, d.get(field)), reverse=direction < 0
            )
        return dict(candidates[0]) if candidates else None

    async def update_one(self, flt, update, upsert=False, **kwargs):
        for doc in self.docs:
            if _match(doc, flt):
                _apply(doc, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            doc = {k: v for k, v in flt.items() if not isinstance(v, dict)}
            _apply(doc, update)
            self.docs.append(doc)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def count_documents(self, flt=None, **kwargs):
        return len([d for d in self.docs if _match(d, flt or {})])

    def find(self, flt=None, projection=None, **kwargs):
        return _FakeCursor([dict(d) for d in self.docs if _match(d, flt or {})])


class FakeDB:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, FakeCollection())


class FakeGuard:
    """Stand-in for GitGuard. Never touches git.

    Constructed explicitly in every test rather than letting the supervisor
    resolve `get_git_guard()`: that is a process-wide singleton, and caching a
    fake db on it would leak into the guard's own test module.
    """

    def __init__(self, project="aria", record=None):
        self.record = record if record is not None else {"project": project}
        self.checkpoints: list[tuple[str, str]] = []
        self.discarded: list[str] = []

    async def get_session(self, session_id):
        return dict(self.record) if self.record else None

    async def checkpoint(self, session_id, reason="interval"):
        self.checkpoints.append((session_id, reason))
        return {"ok": True, "committed": True, "sha": "deadbeef"}

    async def discard(self, session_id):
        self.discarded.append(session_id)
        return {"ok": True, "parked_branch": f"parked/aria/{session_id[:8]}"}


@pytest.fixture(autouse=True)
def _no_real_signal():
    """corsair's .env carries a live break-glass account and signal-cli really
    is listening on :8090; an unpatched send in a test messages Ben for real
    (it happened once). Nailed shut for every test in this module."""
    class _Exploding:
        def __init__(self, *a, **kw):
            raise AssertionError("no test here may open a Signal connection")

    with patch.object(signal_rpc, "httpx", SimpleNamespace(AsyncClient=_Exploding)):
        yield


def _make_supervisor(db=None, **kwargs):
    manager = kwargs.pop("session_manager", None)
    if manager is None:
        manager = MagicMock()
        manager.list_sessions = AsyncMock(return_value=[])
        manager.get_diff = AsyncMock(return_value="")
        manager.send_input = AsyncMock(return_value=True)
        manager.stop_session = AsyncMock(return_value=True)
        manager.resume_session = AsyncMock(return_value=None)
        manager.start_session = AsyncMock(return_value=None)
    notifier = kwargs.pop("notification_service", None)
    if notifier is None:
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value={"queued": True})
    return MetaSupervisor(
        db if db is not None else FakeDB(),
        session_manager=manager,
        notification_service=notifier,
        guard=kwargs.pop("guard", FakeGuard()),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# pi_transcript — parsed against the real schema
# ---------------------------------------------------------------------------

def _session_line(sid="s1", cwd="/home/ben/Development/ProjectAria"):
    return json.dumps({"type": "session", "version": 3, "id": sid,
                       "timestamp": "2026-08-09T12:48:58.338Z", "cwd": cwd})


def _assistant(tool=None, args=None, text="", thinking="", stop="stop",
               usage=None, error=None, ts=1786578392007, extra=None):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking,
                        "thinkingSignature": "reasoning_content"})
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "toolCall", "id": f"tc-{tool}-{ts}", "name": tool,
                        "arguments": args or {}})
    message = {
        "role": "assistant", "content": content, "api": "openai-completions",
        "provider": "ds4", "model": "DS4", "stopReason": stop,
        "timestamp": ts,
        "usage": usage or {"input": 10, "output": 5, "cacheRead": 1,
                           "cacheWrite": 0, "reasoning": 0, "totalTokens": 16},
    }
    if error:
        message["errorMessage"] = error
    if extra:
        message.update(extra)
    return json.dumps({"type": "message", "id": f"m{ts}", "parentId": None,
                       "timestamp": "2026-08-09T12:49:00.000Z", "message": message})


def _tool_result(tool="bash", text="ok", is_error=False, call_id="tc-1"):
    return json.dumps({"type": "message", "id": "r1", "parentId": None,
                       "timestamp": "2026-08-09T12:49:01.000Z",
                       "message": {"role": "toolResult", "toolName": tool,
                                   "toolCallId": call_id, "isError": is_error,
                                   "content": [{"type": "text", "text": text}],
                                   "timestamp": 1786578393000}})


class TestPiTranscriptSchema:
    def test_cwd_slug_matches_the_real_directories(self):
        # Verified against every directory in ~/.pi/agent/sessions on corsair.
        assert pt.cwd_slug("/home/ben") == "--home-ben--"
        assert (pt.cwd_slug("/home/ben/Development/ProjectAria")
                == "--home-ben-Development-ProjectAria--")

    def test_parses_session_model_and_usage(self):
        lines = [
            _session_line(),
            json.dumps({"type": "model_change", "id": "c1", "parentId": None,
                        "timestamp": "2026-08-09T12:48:58.964Z",
                        "provider": "ds4", "modelId": "DS4-Halo"}),
            json.dumps({"type": "thinking_level_change", "id": "t1", "parentId": "c1",
                        "timestamp": "2026-08-09T12:48:58.964Z", "thinkingLevel": "off"}),
            _assistant(text="hello"),
        ]
        t = pt.parse_lines(lines)
        assert t.provider == "ds4" and t.model == "DS4-Halo"
        assert t.cwd == "/home/ben/Development/ProjectAria"
        assert t.usage.total == 16 and t.usage.input == 10
        assert len(t.turns) == 1

    def test_partial_last_line_is_not_corruption(self):
        # A live transcript is being appended to while we read it; the last line
        # is routinely half-written and must not count as malformed.
        lines = [_session_line(), _assistant(text="hi"), '{"type": "mess']
        t = pt.parse_lines(lines)
        assert t.malformed_lines == 0 and len(t.turns) == 1

    def test_broken_middle_line_is_counted_not_fatal(self):
        lines = [_session_line(), "{not json", _assistant(text="hi")]
        t = pt.parse_lines(lines)
        assert t.malformed_lines == 1 and len(t.turns) == 1

    def test_unknown_record_types_and_keys_are_ignored(self):
        lines = [
            json.dumps({"type": "some_future_record", "whatever": 1}),
            _assistant(text="hi", extra={"brandNewField": {"a": 1}}),
        ]
        t = pt.parse_lines(lines)
        assert len(t.turns) == 1

    @pytest.mark.asyncio
    async def test_missing_file_is_none_not_an_error(self):
        assert await pt.load_transcript("no-such-session-id") is None

    @pytest.mark.asyncio
    async def test_finds_and_parses_a_transcript_on_disk(self, tmp_path):
        root = tmp_path / "sessions"
        directory = root / pt.cwd_slug("/home/ben/Development/ProjectAria")
        directory.mkdir(parents=True)
        path = directory / "2026-08-09T12-48-58-338Z_abc-123.jsonl"
        path.write_text("\n".join([_session_line(sid="abc-123"),
                                   _assistant(tool="bash", args={"command": "ls"})]))
        t = await pt.load_transcript("abc-123", "/home/ben/Development/ProjectAria", root=root)
        assert t is not None and t.path == path
        assert t.recent_tool_calls()[0][0] == "bash"

    @pytest.mark.asyncio
    async def test_found_by_glob_when_the_cwd_is_a_worktree(self, tmp_path):
        # A guarded session's cwd is <repo>/.worktrees/<project>-<sid8>, so the
        # slug guess misses exactly the sessions the supervisor cares about.
        root = tmp_path / "sessions"
        directory = root / pt.cwd_slug("/home/ben/Development/ProjectAria/.worktrees/aria-abc12345")
        directory.mkdir(parents=True)
        (directory / "2026-08-09T00-00-00-000Z_abc-123.jsonl").write_text(
            _session_line(sid="abc-123")
        )
        t = await pt.load_transcript("abc-123", "/home/ben/Development/ProjectAria", root=root)
        assert t is not None


class TestPiTranscriptSignals:
    def test_identical_tool_calls_in_a_row_are_a_loop(self):
        lines = [_assistant(tool="bash", args={"command": "make test"}, ts=i)
                 for i in range(4)]
        t = pt.parse_lines(lines)
        assert t.repeating_tool_call(4) == ("bash", t.tool_calls[0].args_hash, 4)

    def test_same_tool_different_arguments_is_not_a_loop(self):
        lines = [_assistant(tool="bash", args={"command": f"step {i}"}, ts=i)
                 for i in range(4)]
        assert pt.parse_lines(lines).repeating_tool_call(4) is None

    def test_argument_key_order_does_not_change_the_hash(self):
        a = pt.parse_lines([_assistant(tool="edit", args={"a": 1, "b": 2})])
        b = pt.parse_lines([_assistant(tool="edit", args={"b": 2, "a": 1})])
        assert a.tool_calls[0].args_hash == b.tool_calls[0].args_hash

    def test_alternating_pair(self):
        lines = []
        for i in range(3):
            lines.append(_assistant(tool="read", args={"path": "/x"}, ts=2 * i))
            lines.append(_assistant(tool="bash", args={"command": "pytest"}, ts=2 * i + 1))
        t = pt.parse_lines(lines)
        assert t.alternating_tool_pair(6) == ("read", "bash", 6)

    def test_alternating_needs_two_distinct_calls(self):
        lines = [_assistant(tool="read", args={"path": "/x"}, ts=i) for i in range(6)]
        assert pt.parse_lines(lines).alternating_tool_pair(6) is None

    def test_trailing_monologue_counts_turns_since_the_last_tool_call(self):
        lines = [_assistant(tool="bash", args={"command": "ls"}, ts=0),
                 _assistant(text="thinking about it", ts=1),
                 _assistant(text="still thinking", ts=2),
                 _assistant(text="one more", ts=3)]
        assert pt.parse_lines(lines).trailing_monologue_turns() == 3

    def test_provider_errors_do_not_read_as_monologue(self):
        # Four "Request timed out." turns is a dead endpoint, not a reasoning
        # loop — labelling it monologue sends the ladder down the wrong branch.
        lines = [_assistant(tool="bash", args={"command": "ls"}, ts=0),
                 _assistant(stop="error", error="Request timed out.", ts=1),
                 _assistant(stop="error", error="Request timed out.", ts=2)]
        t = pt.parse_lines(lines)
        assert t.trailing_monologue_turns() == 0
        assert t.repeated_error(2) == ("Request timed out.", 2)

    def test_tool_result_errors_feed_the_repeated_error_signal(self):
        lines = [_tool_result(text="bash: boom: command not found", is_error=True),
                 _tool_result(text="bash: boom: command not found", is_error=True),
                 _tool_result(text="bash: boom: command not found", is_error=True)]
        assert pt.parse_lines(lines).repeated_error(3)[1] == 3

    def test_distinct_errors_are_not_a_repeat(self):
        lines = [_tool_result(text="error one", is_error=True),
                 _tool_result(text="error two", is_error=True),
                 _tool_result(text="error three", is_error=True)]
        assert pt.parse_lines(lines).repeated_error(3) is None


# ---------------------------------------------------------------------------
# Long-running-child exemption (OpenHands #5355)
# ---------------------------------------------------------------------------

def _fake_proc(tmp_path, entries, boot=1000.0):
    """entries: [(pid, comm, cwd, age_seconds)] against a synthetic /proc."""
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(f"cpu 1 2 3\nbtime {int(boot)}\n")
    hz = os.sysconf("SC_CLK_TCK") or 100
    for pid, comm, cwd, age in entries:
        d = proc / str(pid)
        d.mkdir()
        os.symlink(str(cwd), str(d / "cwd"))
        start_ticks = int((boot + 10_000 - age - boot) * hz)
        fields = ["S", "1"] + ["0"] * 30
        fields[11] = "5"   # utime
        fields[12] = "5"   # stime
        fields[19] = str(start_ticks)
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))
    return proc


class TestLongRunningChildExemption:
    def test_a_test_run_in_the_worktree_exempts(self, tmp_path):
        work = tmp_path / "wt"
        work.mkdir()
        proc = _fake_proc(tmp_path, [(101, "pytest", work, 300)])
        found = long_running_children(str(work), 60, proc_root=str(proc),
                                      boot_time=1000.0, now=1000.0 + 10_000)
        assert [f["comm"] for f in found] == ["pytest"]

    def test_the_agent_itself_does_not_exempt(self, tmp_path):
        # pi/node/the pane shell always sit in the worktree; counting them would
        # exempt every session and disable the supervisor entirely.
        work = tmp_path / "wt"
        work.mkdir()
        proc = _fake_proc(tmp_path, [(101, "pi", work, 900), (102, "bash", work, 900)])
        assert long_running_children(str(work), 60, proc_root=str(proc),
                                     boot_time=1000.0, now=1000.0 + 10_000) == []

    def test_a_young_process_does_not_exempt(self, tmp_path):
        work = tmp_path / "wt"
        work.mkdir()
        proc = _fake_proc(tmp_path, [(101, "make", work, 5)])
        assert long_running_children(str(work), 60, proc_root=str(proc),
                                     boot_time=1000.0, now=1000.0 + 10_000) == []

    def test_a_process_outside_the_worktree_does_not_exempt(self, tmp_path):
        work = tmp_path / "wt"
        other = tmp_path / "elsewhere"
        work.mkdir()
        other.mkdir()
        proc = _fake_proc(tmp_path, [(101, "pytest", other, 900)])
        assert long_running_children(str(work), 60, proc_root=str(proc),
                                     boot_time=1000.0, now=1000.0 + 10_000) == []

    def test_process_name_with_spaces_and_parens_is_parsed(self, tmp_path):
        # comm is parsed with rfind(')') on purpose: splitting on whitespace
        # shifts every later field and reads garbage as the start time.
        work = tmp_path / "wt"
        work.mkdir()
        proc = _fake_proc(tmp_path, [(101, "my (weird) proc", work, 900)])
        found = long_running_children(str(work), 60, proc_root=str(proc),
                                      boot_time=1000.0, now=1000.0 + 10_000)
        assert found and found[0]["comm"] == "my (weird) proc"

    @pytest.mark.asyncio
    async def test_signals_are_suppressed_while_a_child_runs(self):
        sup = _make_supervisor()
        session = {"_id": "s1", "workspace": "/tmp/wt", "loop_nudges": 9,
                   "created_at": _utc(-10)}
        pane = {"repeated_error": {"line": "boom", "count": 5}}
        with patch("aria.steward.supervisor.long_running_children",
                   return_value=[{"pid": 1, "comm": "pytest", "elapsed_seconds": 400.0}]):
            signals, exemption = await sup.collect_signals(session, pane)
        assert signals == [] and exemption and "pytest" in exemption


# ---------------------------------------------------------------------------
# Watchdog-side signal observation (the pane half of §6.1)
# ---------------------------------------------------------------------------

class TestWatchdogSignals:
    def test_repeated_error_line_needs_the_threshold(self):
        text = "\n".join(["ConnectionError: connection refused to :8104"] * 3)
        assert repeated_error_line(text, 3)[1] == 3
        assert repeated_error_line(text, 4) is None

    def test_short_and_non_error_lines_are_ignored(self):
        # Prompts, spinners and box-drawing repeat constantly in a tmux pane.
        text = "\n".join(["> ", "...", "building the index for the project"] * 5)
        assert repeated_error_line(text, 3) is None

    def test_reply_hash_ignores_trailing_whitespace(self):
        assert reply_hash("a\nb   \n") == reply_hash("a\nb\n")

    @pytest.mark.asyncio
    async def test_check_sessions_publishes_signals_without_changing_behaviour(self):
        error = "TimeoutError: request timed out after 30s"
        output = "\n".join([error] * 4)
        manager = MagicMock()
        session = {"_id": "s1", "workspace": "/tmp/wt", "loop_nudges": 0}
        manager.list_sessions = AsyncMock(return_value=[session])
        manager.get_output = AsyncMock(return_value=output)
        notifier = MagicMock()
        notifier.notify = AsyncMock()
        wd = CodingWatchdog(db=MagicMock(), session_manager=manager,
                            notification_service=notifier, review_service=None)
        wd.budget_guard = MagicMock()
        wd.budget_guard.check = MagicMock(return_value=None)

        await wd._check_sessions()          # first tick only seeds the hash
        assert wd.signals("s1") == {}
        wd._session_state["s1"]["last_changed_at"] = _utc(-settings.coding_stall_seconds - 5)
        await wd._check_sessions()

        signals = wd.signals("s1")
        assert signals["stalled"] is True
        assert signals["repeated_error"]["count"] >= settings.meta_repeated_error_threshold
        # The pre-existing stall alert must still fire — the observation hook is
        # additive, not a replacement.
        assert any(call.kwargs.get("event_type", "").startswith("stalled:")
                   for call in notifier.notify.await_args_list)

    @pytest.mark.asyncio
    async def test_nudge_echo_needs_two_identical_replies_to_two_nudges(self):
        manager = MagicMock()
        session = {"_id": "s1", "workspace": "/tmp/wt", "loop_nudges": 1}
        manager.list_sessions = AsyncMock(return_value=[session])
        manager.get_output = AsyncMock(return_value="I will now continue the task.")
        notifier = MagicMock()
        notifier.notify = AsyncMock()
        wd = CodingWatchdog(db=MagicMock(), session_manager=manager,
                            notification_service=notifier, review_service=None)
        wd.budget_guard = MagicMock()
        wd.budget_guard.check = MagicMock(return_value=None)

        await wd._check_sessions()
        state = wd._session_state["s1"]
        state["last_changed_at"] = _utc(-settings.coding_stall_seconds - 5)
        await wd._check_sessions()
        assert wd.signals("s1")["nudge_echo"] is None  # one sample is not an echo

        session["loop_nudges"] = 2
        state["last_changed_at"] = _utc(-settings.coding_stall_seconds - 5)
        await wd._check_sessions()
        assert wd.signals("s1")["nudge_echo"] is not None

    @pytest.mark.asyncio
    async def test_idle_ticks_alone_never_produce_an_echo(self):
        # Sampling every tick would compare a reply against itself and report an
        # echo for any quiet session.
        manager = MagicMock()
        session = {"_id": "s1", "workspace": "/tmp/wt", "loop_nudges": 0}
        manager.list_sessions = AsyncMock(return_value=[session])
        manager.get_output = AsyncMock(return_value="waiting")
        notifier = MagicMock()
        notifier.notify = AsyncMock()
        wd = CodingWatchdog(db=MagicMock(), session_manager=manager,
                            notification_service=notifier, review_service=None)
        wd.budget_guard = MagicMock()
        wd.budget_guard.check = MagicMock(return_value=None)
        await wd._check_sessions()
        for _ in range(4):
            wd._session_state["s1"]["last_changed_at"] = _utc(
                -settings.coding_stall_seconds - 5
            )
            await wd._check_sessions()
        assert wd.signals("s1")["nudge_echo"] is None


# ---------------------------------------------------------------------------
# Signal collection
# ---------------------------------------------------------------------------

class TestSignalCollection:
    @pytest.mark.asyncio
    async def test_no_diff_after_enough_nudges(self, monkeypatch):
        monkeypatch.setattr(settings, "meta_no_diff_nudges", 2)
        sup = _make_supervisor()
        session = {"_id": "s1", "workspace": "/tmp/wt", "loop_nudges": 0,
                   "created_at": _utc(-30)}
        with patch("aria.steward.supervisor.long_running_children", return_value=[]):
            signals, _ = await sup.collect_signals(session, {})
            assert signals == []
            session["loop_nudges"] = 2
            signals, _ = await sup.collect_signals(session, {})
        assert [s.name for s in signals] == ["no_diff"]

    @pytest.mark.asyncio
    async def test_a_changed_diff_resets_the_nudge_accounting(self, monkeypatch):
        monkeypatch.setattr(settings, "meta_no_diff_nudges", 2)
        sup = _make_supervisor()
        session = {"_id": "s1", "workspace": "/tmp/wt", "loop_nudges": 2,
                   "created_at": _utc(-30)}
        with patch("aria.steward.supervisor.long_running_children", return_value=[]):
            await sup.collect_signals(session, {})
            sup.session_manager.get_diff = AsyncMock(return_value="+ new line")
            signals, _ = await sup.collect_signals(session, {})
        assert signals == []

    @pytest.mark.asyncio
    async def test_a_guard_checkpoint_counts_as_progress(self, monkeypatch):
        # git diff alone empties on every checkpoint commit, so a productive
        # session would look frozen exactly when it did the most work.
        monkeypatch.setattr(settings, "meta_no_diff_nudges", 2)
        db = FakeDB()
        sup = _make_supervisor(db)
        session = {"_id": "s1", "workspace": "/tmp/wt", "loop_nudges": 2,
                   "created_at": _utc(-30)}
        with patch("aria.steward.supervisor.long_running_children", return_value=[]):
            await sup.collect_signals(session, {})
            await db["guard_checkpoints"].insert_one(
                {"session_id": "s1", "sha": "abc123", "at": _utc()}
            )
            signals, _ = await sup.collect_signals(session, {})
        assert signals == []

    @pytest.mark.asyncio
    async def test_wall_clock_budget(self, monkeypatch):
        db = FakeDB()
        await db.projects.insert_one(
            {"slug": "aria", "path": "/tmp/wt",
             "charter": {"purpose": "x", "budget": {"session_minutes": 5}}}
        )
        sup = _make_supervisor(db)
        session = {"_id": "s1", "workspace": "/tmp/wt", "loop_nudges": 0,
                   "created_at": _utc(-3600)}
        with patch("aria.steward.supervisor.long_running_children", return_value=[]):
            signals, _ = await sup.collect_signals(session, {})
        assert [s.name for s in signals] == ["budget_wall_clock"]


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

def _stuck() -> list[Signal]:
    return [Signal("tool_loop", "called bash with identical arguments 4x", "transcript")]


class TestLadder:
    @pytest.mark.asyncio
    async def test_l0_logs_and_arms_the_nudge_rung(self):
        db = FakeDB()
        sup = _make_supervisor(db)
        session = {"_id": "s1", "workspace": "/tmp/wt"}
        record = await sup.escalate(session, _stuck())
        assert record["rung"] == L0_LOG and record["action"] == "log"
        assert sup._state["s1"]["rung"] == L1_NUDGE
        assert sup.session_manager.send_input.await_count == 0
        # Every rung leaves an alert, a guard event and an escalation row.
        assert sup.notifier.notify.await_count == 1
        assert len(db["guard_events"].docs) == 1
        assert len(db["meta_escalations"].docs) == 1

    @pytest.mark.asyncio
    async def test_rung_alerts_are_info_and_never_reach_ben(self):
        sup = _make_supervisor()
        await sup.escalate({"_id": "s1", "workspace": "/tmp/wt"}, _stuck())
        kwargs = sup.notifier.notify.await_args_list[0].kwargs
        assert kwargs["severity"] == "info" and kwargs["needs_human"] is False

    @pytest.mark.asyncio
    async def test_debounce_gives_a_rung_time_to_work(self):
        sup = _make_supervisor()
        session = {"_id": "s1", "workspace": "/tmp/wt"}
        assert await sup.escalate(session, _stuck()) is not None
        assert await sup.escalate(session, _stuck()) is None  # too soon

    @pytest.mark.asyncio
    async def test_l1_nudge_names_the_specific_signal(self):
        sup = _make_supervisor()
        session = {"_id": "s1", "workspace": "/tmp/wt"}
        sup._state["s1"] = {**sup._state.get("s1", {}), "rung": L1_NUDGE,
                            "nudges": 0, "history": [], "last_action_at": None}
        record = await sup.escalate(session, _stuck())
        assert record["action"] == "nudge" and record["ok"] is True
        text = sup.session_manager.send_input.await_args.args[1]
        assert "tool_loop" in text and "identical arguments" in text
        # A generic "keep going" is what the Ralph loop already sends.
        assert "Do NOT repeat the last action" in text

    @pytest.mark.asyncio
    async def test_nudges_are_capped_then_the_ladder_advances(self):
        sup = _make_supervisor()
        session = {"_id": "s1", "workspace": "/tmp/wt"}
        state = {"rung": L1_NUDGE, "nudges": MAX_L1_NUDGES, "history": [],
                 "last_action_at": None}
        sup._state["s1"] = state
        record = await sup.escalate(session, _stuck())
        assert record["ok"] is False and "nudges spent" in record["reason"]
        assert state["rung"] == L2_RESTART

    @pytest.mark.asyncio
    async def test_meta_nudges_are_counted_separately_from_ralph_nudges(self):
        # Consuming loop_nudges would silently shorten a healthy Ralph loop.
        db = FakeDB()
        await db.coding_sessions.insert_one({"_id": "s1", "loop_nudges": 1})
        sup = _make_supervisor(db)
        sup._state["s1"] = {"rung": L1_NUDGE, "nudges": 0, "history": [],
                            "last_action_at": None}
        await sup.escalate({"_id": "s1", "workspace": "/tmp/wt"}, _stuck())
        doc = await db.coding_sessions.find_one({"_id": "s1"})
        assert doc["meta_nudges"] == 1 and doc["loop_nudges"] == 1

    @pytest.mark.asyncio
    async def test_l2_restart_carries_a_reflexion_note_and_the_ladder_forward(self):
        sup = _make_supervisor()
        sup.session_manager.resume_session = AsyncMock(return_value={"_id": "s2"})
        sup._state["s1"] = {"rung": L2_RESTART, "nudges": 3,
                            "history": [{"rung": 1, "action": "nudge"}],
                            "last_action_at": None}
        written = {}

        async def _write(db, session_id, workspace, current_step=None, notes=None):
            written.update({"session_id": session_id, "notes": notes})
            return SimpleNamespace()

        with patch("aria.agents.checkpoint.write_checkpoint", new=_write):
            record = await sup.escalate({"_id": "s1", "workspace": "/tmp/wt"}, _stuck())

        assert record["action"] == "restart" and record["new_session_id"] == "s2"
        assert "The last attempt failed" in written["notes"]
        assert "tool_loop" in written["notes"]
        # The child must NOT start the ladder over — that is the loop the ladder
        # exists to bound.
        assert sup._state["s2"]["rung"] == L3_REROUTE
        assert sup._state["s2"]["parent_session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_l2_without_a_checkpoint_falls_through(self):
        sup = _make_supervisor()
        sup.session_manager.resume_session = AsyncMock(return_value=None)
        sup._state["s1"] = {"rung": L2_RESTART, "nudges": 3, "history": [],
                            "last_action_at": None}
        with patch("aria.agents.checkpoint.write_checkpoint", new=AsyncMock()):
            record = await sup.escalate({"_id": "s1", "workspace": "/tmp/wt"}, _stuck())
        assert record["ok"] is False and record["reason"] == "no resumable checkpoint"
        assert sup._state["s1"]["rung"] == L3_REROUTE

    @pytest.mark.asyncio
    async def test_l3_reroute_refused_when_the_charter_forbids_the_tier(self):
        db = FakeDB()
        await db.projects.insert_one(
            {"slug": "aria", "path": "/tmp/wt",
             "charter": {"purpose": "x", "tiers_allowed": ["local"]}}
        )
        sup = _make_supervisor(db)
        sup._state["s1"] = {"rung": L3_REROUTE, "nudges": 3, "history": [],
                            "last_action_at": None}
        record = await sup.escalate(
            {"_id": "s1", "workspace": "/tmp/wt", "backend": "pi-code", "llm": "ds4"},
            _stuck(),
        )
        assert record["ok"] is False and "tiers" in record["reason"]
        assert sup.session_manager.start_session.await_count == 0

    @pytest.mark.asyncio
    async def test_l3_reroute_climbs_one_tier_when_allowed(self):
        db = FakeDB()
        await db.projects.insert_one(
            {"slug": "aria", "path": "/tmp/wt",
             "charter": {"purpose": "x", "tiers_allowed": ["local", "cloud"]}}
        )
        sup = _make_supervisor(db)
        sup.session_manager.start_session = AsyncMock(return_value={"_id": "s2"})
        sup._state["s1"] = {"rung": L3_REROUTE, "nudges": 3, "history": [],
                            "last_action_at": None}
        record = await sup.escalate(
            {"_id": "s1", "workspace": "/tmp/wt", "backend": "pi-code", "llm": "ds4",
             "prompt": "do the thing"},
            _stuck(),
        )
        assert record["ok"] is True
        assert (record["from_tier"], record["to_tier"]) == ("local", "cloud")
        prompt = sup.session_manager.start_session.await_args.kwargs["prompt"]
        assert "The last attempt failed" in prompt and "do the thing" in prompt

    @pytest.mark.asyncio
    async def test_l3_will_not_reroute_into_a_cooling_down_cloud_tier(self):
        db = FakeDB()
        await db.projects.insert_one(
            {"slug": "aria", "path": "/tmp/wt",
             "charter": {"purpose": "x", "tiers_allowed": ["local", "cloud"]}}
        )
        sup = _make_supervisor(db)
        sup._state["s1"] = {"rung": L3_REROUTE, "nudges": 3, "history": [],
                            "last_action_at": None}
        with patch("aria.agents.routing.get_cooldown",
                   new=AsyncMock(return_value=_utc(600))):
            record = await sup.escalate(
                {"_id": "s1", "workspace": "/tmp/wt", "backend": "pi-code", "llm": "ds4"},
                _stuck(),
            )
        assert record["ok"] is False and "cooling down" in record["reason"]

    @pytest.mark.asyncio
    async def test_l4_proposes_a_split_rather_than_spawning_one(self):
        sup = _make_supervisor()
        sup._state["s1"] = {"rung": L4_DECOMPOSE, "nudges": 3, "history": [],
                            "last_action_at": None}
        record = await sup.escalate(
            {"_id": "s1", "workspace": "/tmp/wt", "prompt": "big task"}, _stuck()
        )
        assert record["action"] == "decompose"
        assert record["proposal"]["original_prompt"] == "big task"
        assert sup.session_manager.start_session.await_count == 0

    @pytest.mark.asyncio
    async def test_l5_parks_through_the_guard_and_raises(self):
        db = FakeDB()
        await db.coding_sessions.insert_one({"_id": "s1"})
        guard = FakeGuard(project="aria")
        sup = _make_supervisor(db, guard=guard)
        sup._state["s1"] = {"rung": L5_PARK, "nudges": 3, "history": [],
                            "last_action_at": None}
        record = await sup.escalate({"_id": "s1", "workspace": "/tmp/wt"}, _stuck())

        assert record["action"] == "park" and record["raised"] is True
        # Checkpoint BEFORE discard: the branch is the postmortem material.
        assert guard.checkpoints and guard.discarded == ["s1"]
        assert record["parked_branch"].startswith("parked/")
        raise_call = sup.notifier.notify.await_args_list[-1].kwargs
        assert raise_call["needs_human"] is True and raise_call["severity"] == "high"
        assert raise_call["proposal"]["kind"] == "parked_session"

    @pytest.mark.asyncio
    async def test_the_cap_ends_at_park_never_in_silence(self, monkeypatch):
        # With the ladder capped at L1, a still-stuck session must park rather
        # than sit at the cap forever.
        monkeypatch.setattr(settings, "meta_ladder_max_rung", 1)
        db = FakeDB()
        await db.coding_sessions.insert_one({"_id": "s1"})
        sup = _make_supervisor(db)
        sup._state["s1"] = {"rung": L2_RESTART, "nudges": 3, "history": [],
                            "last_action_at": None}
        record = await sup.escalate({"_id": "s1", "workspace": "/tmp/wt"}, _stuck())
        assert record["rung"] == L5_PARK and record["action"] == "park"

    @pytest.mark.asyncio
    async def test_a_nudge_that_changed_nothing_does_not_earn_another(self):
        # "<=3 nudges, each must show diff progress" — the no_diff signal IS the
        # report that the last nudge changed nothing.
        sup = _make_supervisor()
        state = {"rung": L1_NUDGE, "nudges": 1, "history": [], "last_action_at": None}
        sup._state["s1"] = state
        signals = [Signal("no_diff", "2 nudges with no change to the worktree", "git")]
        record = await sup.escalate({"_id": "s1", "workspace": "/tmp/wt"}, signals)
        assert record["ok"] is False and "no change" in record["reason"]
        assert state["rung"] == L2_RESTART
        assert sup.session_manager.send_input.await_count == 0

    @pytest.mark.asyncio
    async def test_full_climb_reaches_park_and_stops(self, monkeypatch):
        monkeypatch.setattr(settings, "coding_stall_seconds", 0)  # no debounce
        db = FakeDB()
        await db.coding_sessions.insert_one({"_id": "s1"})
        sup = _make_supervisor(db)
        sup.session_manager.resume_session = AsyncMock(return_value=None)
        session = {"_id": "s1", "workspace": "/tmp/wt", "backend": "pi-code"}
        actions = []
        with patch("aria.agents.checkpoint.write_checkpoint", new=AsyncMock()):
            for _ in range(6 + MAX_L1_NUDGES):
                record = await sup.escalate(session, _stuck())
                actions.append(record["action"])
        # One rung per tick: the 4th "nudge" is the refusal that advances to L2.
        assert actions == (["log"] + ["nudge"] * (MAX_L1_NUDGES + 1)
                           + ["restart", "reroute", "decompose", "park"])
        assert sup._state["s1"].get("terminal") is True


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_too_many_raises_proposes_a_pause_and_stops_raising(self, monkeypatch):
        monkeypatch.setattr(settings, "meta_raises_per_project_per_day", 2)
        db = FakeDB()
        await db.coding_sessions.insert_one({"_id": "s9"})
        for i in range(3):
            await db["meta_escalations"].insert_one(
                {"project": "aria", "raised": True, "at": _utc(-60 * i)}
            )
        planning = MagicMock()
        planning.propose_pause = AsyncMock(return_value=True)
        sup = _make_supervisor(db, planning_service=planning)
        sup._state["s9"] = {"rung": L5_PARK, "nudges": 3, "history": [],
                            "last_action_at": None}
        record = await sup.escalate({"_id": "s9", "workspace": "/tmp/wt"}, _stuck())

        assert record["raised"] is False        # the park itself no longer pages
        assert record["circuit_breaker"]["raises_24h"] == 3
        planning.propose_pause.assert_awaited_once()
        breaker = [c.kwargs for c in sup.notifier.notify.await_args_list
                   if c.kwargs.get("event_type") == "circuit_breaker"]
        assert breaker and breaker[0]["dedup_key"] == "meta:circuit:aria"
        assert breaker[0]["needs_human"] is True

    @pytest.mark.asyncio
    async def test_the_nth_raise_still_reaches_ben(self, monkeypatch):
        # The budget is "N raises per project per day"; the breaker fires on the
        # one AFTER that, not on the last allowed one.
        monkeypatch.setattr(settings, "meta_raises_per_project_per_day", 3)
        db = FakeDB()
        await db.coding_sessions.insert_one({"_id": "s9"})
        for i in range(2):
            await db["meta_escalations"].insert_one(
                {"project": "aria", "raised": True, "at": _utc(-60 * i)}
            )
        sup = _make_supervisor(db)
        sup._state["s9"] = {"rung": L5_PARK, "nudges": 3, "history": [],
                            "last_action_at": None}
        record = await sup.escalate({"_id": "s9", "workspace": "/tmp/wt"}, _stuck())
        assert record["raised"] is True and record.get("circuit_breaker") is None

    @pytest.mark.asyncio
    async def test_old_raises_do_not_trip_the_breaker(self, monkeypatch):
        monkeypatch.setattr(settings, "meta_raises_per_project_per_day", 2)
        db = FakeDB()
        await db.coding_sessions.insert_one({"_id": "s9"})
        for i in range(5):
            await db["meta_escalations"].insert_one(
                {"project": "aria", "raised": True, "at": _utc(-86400 * 2)}
            )
        sup = _make_supervisor(db)
        sup._state["s9"] = {"rung": L5_PARK, "nudges": 3, "history": [],
                            "last_action_at": None}
        record = await sup.escalate({"_id": "s9", "workspace": "/tmp/wt"}, _stuck())
        assert record["raised"] is True


# ---------------------------------------------------------------------------
# Crash-as-completed
# ---------------------------------------------------------------------------

class TestCrashAsCompleted:
    @pytest.mark.asyncio
    async def test_instant_exit_with_no_diff_becomes_failed(self):
        db = FakeDB()
        start = _utc(-300)
        await db.coding_sessions.insert_one({
            "_id": "s1", "status": "completed", "exit_code": None,
            "created_at": start, "completed_at": start + timedelta(seconds=3),
            "workspace": "/tmp/wt",
        })
        sup = _make_supervisor(db)
        corrected = await sup.check_crash_as_completed()
        doc = await db.coding_sessions.find_one({"_id": "s1"})
        assert corrected and doc["status"] == "failed"
        assert doc["meta_crash_corrected"] is True and "crash, not completion" in doc["error"]

    @pytest.mark.asyncio
    async def test_a_session_that_produced_a_diff_is_left_completed(self):
        db = FakeDB()
        start = _utc(-300)
        await db.coding_sessions.insert_one({
            "_id": "s1", "status": "completed", "exit_code": None,
            "created_at": start, "completed_at": start + timedelta(seconds=3),
            "workspace": "/tmp/wt",
        })
        sup = _make_supervisor(db)
        sup.session_manager.get_diff = AsyncMock(return_value="+ real work")
        assert await sup.check_crash_as_completed() == []
        assert (await db.coding_sessions.find_one({"_id": "s1"}))["status"] == "completed"

    @pytest.mark.asyncio
    async def test_a_guard_checkpoint_proves_the_session_did_something(self):
        db = FakeDB()
        start = _utc(-300)
        await db.coding_sessions.insert_one({
            "_id": "s1", "status": "completed", "exit_code": None,
            "created_at": start, "completed_at": start + timedelta(seconds=3),
        })
        await db["guard_checkpoints"].insert_one({"session_id": "s1", "sha": "abc"})
        sup = _make_supervisor(db)
        assert await sup.check_crash_as_completed() == []

    @pytest.mark.asyncio
    async def test_a_long_session_is_not_a_crash(self):
        db = FakeDB()
        start = _utc(-3600)
        await db.coding_sessions.insert_one({
            "_id": "s1", "status": "completed", "exit_code": None,
            "created_at": start, "completed_at": start + timedelta(minutes=30),
        })
        sup = _make_supervisor(db)
        assert await sup.check_crash_as_completed() == []

    @pytest.mark.asyncio
    async def test_each_session_is_only_examined_once(self):
        db = FakeDB()
        start = _utc(-300)
        await db.coding_sessions.insert_one({
            "_id": "s1", "status": "completed", "exit_code": None,
            "created_at": start, "completed_at": start + timedelta(seconds=3),
        })
        sup = _make_supervisor(db)
        assert len(await sup.check_crash_as_completed()) == 1
        assert await sup.check_crash_as_completed() == []


# ---------------------------------------------------------------------------
# Cross-kind worker liveness
# ---------------------------------------------------------------------------

class TestWorkerLiveness:
    @pytest.mark.asyncio
    async def test_stale_extraction_cursor_is_informational(self, monkeypatch):
        monkeypatch.setattr(settings, "shells_extraction_enabled", True)
        monkeypatch.setattr(settings, "dream_enabled", False)
        db = FakeDB()
        await db.shell_extraction_state.insert_one(
            {"shell_name": "claude-x", "last_run_at": _utc(-8 * 3600)}
        )
        sup = _make_supervisor(db)
        findings = await sup.check_worker_liveness()
        assert [f["worker"] for f in findings] == ["shell_extraction"]
        assert findings[0]["needs_human"] is False

    @pytest.mark.asyncio
    async def test_a_never_run_extraction_is_not_reported(self, monkeypatch):
        monkeypatch.setattr(settings, "shells_extraction_enabled", True)
        monkeypatch.setattr(settings, "dream_enabled", False)
        sup = _make_supervisor(FakeDB())
        assert await sup.check_worker_liveness() == []

    @pytest.mark.asyncio
    async def test_a_disabled_worker_never_pages(self, monkeypatch):
        # Same rule as the stopped-on-purpose model servers: a capability that
        # is switched off is not an incident.
        monkeypatch.setattr(settings, "shells_extraction_enabled", False)
        monkeypatch.setattr(settings, "dream_enabled", False)
        db = FakeDB()
        await db.shell_extraction_state.insert_one(
            {"shell_name": "claude-x", "last_run_at": _utc(-99 * 3600)}
        )
        sup = _make_supervisor(db)
        assert await sup.check_worker_liveness() == []

    @pytest.mark.asyncio
    async def test_stalled_research_run(self, monkeypatch):
        monkeypatch.setattr(settings, "shells_extraction_enabled", False)
        monkeypatch.setattr(settings, "dream_enabled", False)
        db = FakeDB()
        await db.research_runs.insert_one(
            {"_id": "r1", "status": "running", "updated_at": _utc(-4 * 3600)}
        )
        sup = _make_supervisor(db)
        findings = await sup.check_worker_liveness()
        assert [f["worker"] for f in findings] == ["research"]

    @pytest.mark.asyncio
    async def test_undelivered_alerts_are_needs_human(self, monkeypatch):
        monkeypatch.setattr(settings, "shells_extraction_enabled", False)
        monkeypatch.setattr(settings, "dream_enabled", False)
        db = FakeDB()
        await db.alerts.insert_one({
            "_id": "a1", "needs_human": True, "acked": False, "delivered_at": None,
            "created_at": _utc(-3600), "dedup_key": "selfcheck|degraded",
        })
        sup = _make_supervisor(db)
        findings = await sup.check_worker_liveness()
        assert findings[0]["worker"] == "relay"
        assert findings[0]["needs_human"] is True
        call = sup.notifier.notify.await_args_list[-1].kwargs
        assert call["needs_human"] is True and call["event_type"] == "relay_undelivered"

    @pytest.mark.asyncio
    async def test_the_relay_alert_never_sustains_itself(self, monkeypatch):
        monkeypatch.setattr(settings, "shells_extraction_enabled", False)
        monkeypatch.setattr(settings, "dream_enabled", False)
        db = FakeDB()
        await db.alerts.insert_one({
            "_id": "a1", "needs_human": True, "acked": False, "delivered_at": None,
            "created_at": _utc(-3600), "dedup_key": "meta:relay:undelivered",
        })
        sup = _make_supervisor(db)
        assert await sup.check_worker_liveness() == []

    @pytest.mark.asyncio
    async def test_a_delivered_alert_is_not_a_finding(self, monkeypatch):
        monkeypatch.setattr(settings, "shells_extraction_enabled", False)
        monkeypatch.setattr(settings, "dream_enabled", False)
        db = FakeDB()
        await db.alerts.insert_one({
            "_id": "a1", "needs_human": True, "acked": False,
            "delivered_at": _utc(-60), "created_at": _utc(-3600),
        })
        sup = _make_supervisor(db)
        assert await sup.check_worker_liveness() == []


# ---------------------------------------------------------------------------
# Worker shape
# ---------------------------------------------------------------------------

class TestWorkerShape:
    def test_status_before_start(self):
        sup = _make_supervisor()
        status = sup.status()
        assert status["running"] is False and status["tracked_sessions"] == 0

    @pytest.mark.asyncio
    async def test_start_stop_is_idempotent(self):
        sup = _make_supervisor()
        await sup.start()
        await sup.start()
        assert sup.status()["running"] is True
        await sup.stop()
        await sup.stop()
        assert sup.status()["running"] is False

    @pytest.mark.asyncio
    async def test_evaluate_once_survives_a_broken_session(self, monkeypatch):
        monkeypatch.setattr(settings, "meta_worker_liveness_enabled", False)
        sup = _make_supervisor()
        sup.session_manager.list_sessions = AsyncMock(
            return_value=[{"_id": "bad"}, {"_id": "s2", "workspace": "/tmp/wt"}]
        )
        sup.collect_signals = AsyncMock(side_effect=[RuntimeError("boom"), ([], None)])
        result = await sup.evaluate_once()
        assert result["sessions"] == 2  # the bad one did not abort the pass

    @pytest.mark.asyncio
    async def test_state_is_pruned_for_sessions_that_stopped(self, monkeypatch):
        monkeypatch.setattr(settings, "meta_worker_liveness_enabled", False)
        sup = _make_supervisor()
        sup._state["gone"] = {"rung": 0}
        await sup.evaluate_once()
        assert "gone" not in sup._state

    @pytest.mark.asyncio
    async def test_handed_off_state_survives_pruning(self, monkeypatch):
        # The restart/re-route child inherits the ladder; dropping the parent's
        # handed-off marker on the same tick would lose it.
        monkeypatch.setattr(settings, "meta_worker_liveness_enabled", False)
        sup = _make_supervisor()
        sup._state["parent"] = {"rung": 3, "handed_off": True}
        await sup.evaluate_once()
        assert "parent" in sup._state


class TestTheStopButtonStopsTheLadder:
    """A freeze must stop the supervisor from driving live agents.

    Found by adversarial review: `_rung_nudge` called `send_input()` with no
    gate, so a killswitch or e-stop — often engaged *because* an agent is
    misbehaving — left the supervisor typing into that agent's terminal. The
    spawn-based rungs were covered by `start_session`'s own gates; the nudge was
    not covered by anything.
    """

    @pytest.mark.asyncio
    async def test_an_active_estop_holds_the_ladder(self, monkeypatch):
        from aria.steward.supervisor import MetaSupervisor

        class _Estop:
            async def is_active(self):
                return True

        sup = MetaSupervisor(FakeDB(), session_manager=object(), estop=_Estop())
        assert await sup._halted() == "e-stop active"

        session = {"_id": "s-halt", "status": "running", "workspace": "/tmp/x"}
        sig = [Signal("no_diff", "nothing changed", "progress")]
        assert await sup.escalate(session, sig) is None
        # The debounce window must not be consumed by a hold, or the ladder
        # would skip its next real opportunity to act once the freeze lifts.
        assert sup._state["s-halt"].get("last_action_at") is None

    @pytest.mark.asyncio
    async def test_an_inconclusive_check_does_not_halt(self):
        """A check that cannot be evaluated is not evidence of a freeze."""
        from aria.steward.supervisor import MetaSupervisor

        class _Broken:
            async def is_active(self):
                raise RuntimeError("mongo blip")

        sup = MetaSupervisor(FakeDB(), session_manager=object(), estop=_Broken())
        assert await sup._halted() is None
