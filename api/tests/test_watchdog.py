"""Tests for the coding session watchdog — stuck detection and diagnosis."""

import pytest

from aria.agents.watchdog import (
    StuckReason,
    diagnose_stuck,
)


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
