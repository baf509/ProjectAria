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
