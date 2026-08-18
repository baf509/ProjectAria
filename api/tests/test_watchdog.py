"""Tests for the coding session watchdog — stuck detection and diagnosis."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.agents.watchdog import (
    CodingWatchdog,
    StuckReason,
    diagnose_stuck,
)
from tests.conftest import make_mock_db


class TestDiagnoseStuck:
    def test_empty_output_is_idle(self):
        assert diagnose_stuck("") == StuckReason.IDLE

    def test_rate_limited_429(self):
        output = "Error: 429 Too Many Requests\nRetrying..."
        assert diagnose_stuck(output) == StuckReason.RATE_LIMITED

    def test_rate_limited_keyword(self):
        output = "API rate limit exceeded, please wait"
        assert diagnose_stuck(output) == StuckReason.RATE_LIMITED

    def test_rate_limited_overloaded(self):
        output = "The API is overloaded right now"
        assert diagnose_stuck(output) == StuckReason.RATE_LIMITED

    def test_context_full(self):
        output = "Error: context window limit reached"
        assert diagnose_stuck(output) == StuckReason.CONTEXT_FULL

    def test_context_full_input_too_large(self):
        output = "conversation too long for this model"
        assert diagnose_stuck(output) == StuckReason.CONTEXT_FULL

    def test_retry_loop_repeated_lines(self):
        """Six lines where the last 3 repeat the previous 3."""
        lines = [
            "Error: connection failed",
            "Retrying...",
            "Attempt 2",
            "Error: connection failed",
            "Retrying...",
            "Attempt 2",
        ]
        assert diagnose_stuck("\n".join(lines)) == StuckReason.RETRY_LOOP

    def test_retry_loop_pattern(self):
        output = "attempt 3 of 5 failed, retrying"
        assert diagnose_stuck(output) == StuckReason.RETRY_LOOP

    def test_waiting_input_question_mark(self):
        output = "Please enter your choice:\n> "
        assert diagnose_stuck(output) == StuckReason.WAITING_INPUT

    def test_waiting_input_dollar_prompt(self):
        output = "some output\n$ "
        assert diagnose_stuck(output) == StuckReason.WAITING_INPUT

    def test_unchanged_output_is_idle(self):
        output = "Working on task..."
        assert diagnose_stuck(output, previous_output=output) == StuckReason.IDLE

    def test_unknown_when_output_changed(self):
        assert diagnose_stuck("new output", previous_output="old output") == StuckReason.UNKNOWN

    def test_rate_limit_takes_priority_over_retry(self):
        """Rate limiting is checked first (most urgent)."""
        output = "Error: 429 rate limit\nattempt 3 of 5"
        assert diagnose_stuck(output) == StuckReason.RATE_LIMITED

    def test_only_checks_tail(self):
        """Old lines beyond the 30-line tail should not trigger."""
        old_lines = ["normal output"] * 40
        old_lines[0] = "Error: 429 rate limit"  # Line 0 — beyond tail
        assert diagnose_stuck("\n".join(old_lines)) != StuckReason.RATE_LIMITED


# ---------------------------------------------------------------------------
# _session_state must not grow forever — pruned once a session stops running
# ---------------------------------------------------------------------------

def _make_watchdog(sessions_by_call):
    """CodingWatchdog with a mocked session_manager; list_sessions returns
    each element of `sessions_by_call` in turn, one per _check_sessions() call."""
    session_manager = MagicMock()
    session_manager.list_sessions = AsyncMock(side_effect=sessions_by_call)
    session_manager.get_output = AsyncMock(return_value="working...")
    notification_service = MagicMock()
    notification_service.notify = AsyncMock()
    wd = CodingWatchdog(
        db=make_mock_db(),
        session_manager=session_manager,
        notification_service=notification_service,
        review_service=None,
    )
    return wd


class TestSessionStatePruning:
    @pytest.mark.asyncio
    async def test_state_tracked_while_running(self):
        wd = _make_watchdog([[{"_id": "s1", "status": "running", "workspace": "/w"}]])
        await wd._check_sessions()
        assert "s1" in wd._session_state

    @pytest.mark.asyncio
    async def test_state_pruned_once_no_longer_running(self):
        wd = _make_watchdog([
            [{"_id": "s1", "status": "running", "workspace": "/w"}],
            [],  # s1 completed — next tick's list_sessions(status="running") omits it
        ])
        await wd._check_sessions()
        assert "s1" in wd._session_state
        await wd._check_sessions()
        assert "s1" not in wd._session_state
        assert wd._session_state == {}

    @pytest.mark.asyncio
    async def test_still_running_session_not_pruned(self):
        session = {"_id": "s1", "status": "running", "workspace": "/w"}
        wd = _make_watchdog([[session], [session]])
        await wd._check_sessions()
        await wd._check_sessions()
        assert "s1" in wd._session_state


# ---------------------------------------------------------------------------
# Auto-review window: newest-first, batched report check, capped retries
# ---------------------------------------------------------------------------

class _AsyncCursor:
    """A motor-shaped cursor: sortable, to_list-able, and async-iterable.
    Records sort() calls so tests can assert on them."""

    def __init__(self, docs: list[dict]):
        self._docs = docs
        self.sort_calls: list[tuple] = []

    def sort(self, *a, **k):
        self.sort_calls.append((a, k))
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, length=None):
        return self._docs

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def _make_review_watchdog(terminal_sessions, reports):
    """Watchdog with a review service and scripted terminal-session/report
    collections."""
    db = make_mock_db()
    db.coding_sessions.find = MagicMock(
        return_value=_AsyncCursor(terminal_sessions)
    )
    db.session_reports.find = MagicMock(
        return_value=_AsyncCursor(reports)
    )
    session_manager = MagicMock()
    session_manager.list_sessions = AsyncMock(return_value=[])
    notification_service = MagicMock()
    notification_service.notify = AsyncMock()
    review_service = MagicMock()
    review_service.review_session = AsyncMock()
    wd = CodingWatchdog(
        db=db,
        session_manager=session_manager,
        notification_service=notification_service,
        review_service=review_service,
    )
    return wd


def _terminal(i: int, **extra) -> dict:
    doc = {"_id": f"sess-{i:03d}", "status": "completed"}
    doc.update(extra)
    return doc


@pytest.mark.asyncio
async def test_auto_review_picks_newest_not_oldest():
    """With >100 terminal sessions the window must be the 100 NEWEST —
    the old unsorted query pinned it to the oldest cohort, so new sessions
    were never reviewed at all."""
    # 150 terminal sessions; the find applies to_list(length=100) upstream,
    # so the cursor already carries the newest 100 (the sort is the point
    # under test — assert it is requested newest-first).
    newest = [_terminal(i) for i in range(50, 150)]
    wd = _make_review_watchdog(newest, reports=[])
    await wd._check_sessions()

    find = wd.db.coding_sessions.find
    assert find.called
    cursor = find.return_value
    assert cursor.sort_calls == [(("created_at", -1), {})], \
        "the terminal-session query must sort newest-first"


@pytest.mark.asyncio
async def test_auto_review_reports_checked_in_one_query():
    """The report check is one find over all ids, not one find_one per
    session (the old code did up to 100 round-trips every 5 s)."""
    sessions = [_terminal(i) for i in range(10)]
    reports = [{"session_id": "sess-000"}, {"session_id": "sess-001"}]
    wd = _make_review_watchdog(sessions, reports)
    await wd._check_sessions()

    find = wd.db.session_reports.find
    assert find.call_count == 1
    query = find.call_args.args[0]
    assert query["session_id"]["$in"] == [f"sess-{i:03d}" for i in range(10)]
    # Only the unreported sessions are reviewed — and only the first two:
    # the per-tick cap bounds the subprocess burst (see the next test).
    reviewed = [c.args[0] for c in wd.review_service.review_session.call_args_list]
    assert reviewed == ["sess-002", "sess-003"]


@pytest.mark.asyncio
async def test_auto_review_caps_repeated_failures():
    """A session whose review keeps failing is skipped after three attempts
    instead of being re-probed with subprocesses every 5 s forever."""
    sessions = [_terminal(0, review_failures=3), _terminal(1, review_failures=0)]
    wd = _make_review_watchdog(sessions, reports=[])
    wd.review_service.review_session = AsyncMock(side_effect=RuntimeError("boom"))
    await wd._check_sessions()

    reviewed = [c.args[0] for c in wd.review_service.review_session.call_args_list]
    assert reviewed == ["sess-001"]  # the 3-strike session is skipped
    # The failure is counted; a success would reset it.
    updates = [c for c in wd.db.coding_sessions.update_one.call_args_list]
    inc = [c for c in updates if "$inc" in c.args[1]]
    assert len(inc) == 1
    assert inc[0].args[1]["$inc"] == {"review_failures": 1}


@pytest.mark.asyncio
async def test_auto_review_bounded_per_tick():
    """At most two fresh reviews per tick — each runs git/pytest/ruff/npm/
    eslint subprocesses, and a backlog must not starve the loop."""
    sessions = [_terminal(i) for i in range(10)]
    wd = _make_review_watchdog(sessions, reports=[])
    await wd._check_sessions()
    assert wd.review_service.review_session.call_count == 2
