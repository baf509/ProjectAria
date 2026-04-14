"""Tests for the context budget guard — heuristic detection of context pressure."""

import pytest

from aria.agents.budget_guard import BudgetLevel, ContextBudgetGuard, assess_budget


class TestAssessBudget:
    def test_empty_output_is_ok(self):
        assert assess_budget("") == BudgetLevel.OK

    def test_normal_output_is_ok(self):
        assert assess_budget("Running tests...\nAll 42 passed.") == BudgetLevel.OK

    def test_warn_on_context_getting_large(self):
        output = "Some output\ncontext is getting large\nmore output"
        assert assess_budget(output) == BudgetLevel.WARN

    def test_warn_on_running_low(self):
        output = "running low on context space"
        assert assess_budget(output) == BudgetLevel.WARN

    def test_soft_gate_on_compacting(self):
        output = "compacting context to free space"
        assert assess_budget(output) == BudgetLevel.SOFT_GATE

    def test_soft_gate_on_truncating_conversation(self):
        output = "truncating conversation history"
        assert assess_budget(output) == BudgetLevel.SOFT_GATE

    def test_soft_gate_on_dropped_messages(self):
        output = "dropped 15 earlier messages to fit"
        assert assess_budget(output) == BudgetLevel.SOFT_GATE

    def test_soft_gate_on_summarizing(self):
        output = "summarizing previous messages for brevity"
        assert assess_budget(output) == BudgetLevel.SOFT_GATE

    def test_hard_gate_on_context_exhausted(self):
        output = "context window exhausted"
        assert assess_budget(output) == BudgetLevel.HARD_GATE

    def test_hard_gate_on_input_too_long(self):
        output = "Error: input too long for model"
        assert assess_budget(output) == BudgetLevel.HARD_GATE

    def test_hard_gate_on_request_too_large(self):
        output = "request too large"
        assert assess_budget(output) == BudgetLevel.HARD_GATE

    def test_hard_gate_on_must_reduce(self):
        output = "must reduce input size"
        assert assess_budget(output) == BudgetLevel.HARD_GATE

    def test_hard_overrides_soft(self):
        """When both soft and hard patterns present, hard wins."""
        output = "compacting conversation\ncontext window exhausted"
        assert assess_budget(output) == BudgetLevel.HARD_GATE

    def test_only_checks_tail(self):
        """Old lines beyond the 50-line tail should not trigger."""
        old_lines = ["normal output line"] * 60
        old_lines[0] = "context window exhausted"  # Line 0 — beyond tail
        assert assess_budget("\n".join(old_lines)) == BudgetLevel.OK


class TestContextBudgetGuard:
    def test_first_check_returns_level_if_not_ok(self):
        guard = ContextBudgetGuard()
        result = guard.check("s1", "context window exhausted")
        assert result == BudgetLevel.HARD_GATE

    def test_first_check_returns_none_if_ok(self):
        guard = ContextBudgetGuard()
        result = guard.check("s1", "everything fine")
        assert result is None

    def test_escalation_returns_new_level(self):
        guard = ContextBudgetGuard()
        guard.check("s1", "context is getting large")  # WARN
        result = guard.check("s1", "context window exhausted")  # HARD
        assert result == BudgetLevel.HARD_GATE

    def test_same_level_returns_none(self):
        guard = ContextBudgetGuard()
        guard.check("s1", "context window exhausted")
        result = guard.check("s1", "context window exhausted")
        assert result is None

    def test_de_escalation_allows_re_escalation(self):
        """After de-escalation (output clears), level should be trackable again."""
        guard = ContextBudgetGuard()
        guard.check("s1", "context is getting large")  # WARN
        # Output rotates and pressure clears
        guard.check("s1", "everything fine")  # OK — de-escalation
        # Pressure returns
        result = guard.check("s1", "context is getting large")  # WARN again
        assert result == BudgetLevel.WARN

    def test_independent_sessions(self):
        guard = ContextBudgetGuard()
        guard.check("s1", "context window exhausted")
        result = guard.check("s2", "context window exhausted")
        assert result == BudgetLevel.HARD_GATE  # s2 is independent

    def test_clear_resets_tracking(self):
        guard = ContextBudgetGuard()
        guard.check("s1", "context window exhausted")
        guard.clear("s1")
        result = guard.check("s1", "context window exhausted")
        assert result == BudgetLevel.HARD_GATE  # Treated as new
