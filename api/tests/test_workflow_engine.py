"""Tests for aria.workflows.engine — parameter rendering, conditions, dependencies."""

import pytest

from aria.workflows.engine import WorkflowEngine


@pytest.fixture
def engine():
    """Create a WorkflowEngine with None dependencies for pure method testing."""
    engine = WorkflowEngine.__new__(WorkflowEngine)
    return engine


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------

class TestEvaluateCondition:
    def test_equals_pass(self, engine):
        result = engine._evaluate_condition({"value": "hello", "equals": "hello"})
        assert result["passed"] is True

    def test_equals_fail(self, engine):
        result = engine._evaluate_condition({"value": "hello", "equals": "world"})
        assert result["passed"] is False

    def test_not_equals_pass(self, engine):
        result = engine._evaluate_condition({"value": "a", "not_equals": "b"})
        assert result["passed"] is True

    def test_not_equals_fail(self, engine):
        result = engine._evaluate_condition({"value": "a", "not_equals": "a"})
        assert result["passed"] is False

    def test_contains_pass(self, engine):
        result = engine._evaluate_condition({"value": "hello world", "contains": "world"})
        assert result["passed"] is True

    def test_contains_fail(self, engine):
        result = engine._evaluate_condition({"value": "hello", "contains": "world"})
        assert result["passed"] is False

    def test_matches_regex_pass(self, engine):
        result = engine._evaluate_condition({"value": "abc123", "matches": r"\d+"})
        assert result["passed"] is True

    def test_matches_regex_fail(self, engine):
        result = engine._evaluate_condition({"value": "abc", "matches": r"^\d+$"})
        assert result["passed"] is False

    def test_exists_true(self, engine):
        result = engine._evaluate_condition({"value": "something", "exists": True})
        assert result["passed"] is True

    def test_exists_false(self, engine):
        result = engine._evaluate_condition({"value": None, "exists": True})
        assert result["passed"] is False

    def test_combined_conditions(self, engine):
        result = engine._evaluate_condition({
            "value": "hello world",
            "not_equals": "goodbye",
            "contains": "hello",
        })
        assert result["passed"] is True

    def test_value_returned(self, engine):
        result = engine._evaluate_condition({"value": 42})
        assert result["value"] == 42


# ---------------------------------------------------------------------------
# Dependency validation
# ---------------------------------------------------------------------------

class TestValidateDependencies:
    def test_valid_dependencies(self, engine):
        # Step 2 depends on step 0 and 1 — valid
        engine._validate_dependencies(2, [0, 1])

    def test_self_dependency_raises(self, engine):
        with pytest.raises(ValueError, match="invalid dependencies"):
            engine._validate_dependencies(1, [1])

    def test_forward_dependency_raises(self, engine):
        with pytest.raises(ValueError, match="invalid dependencies"):
            engine._validate_dependencies(0, [1])

    def test_negative_dependency_raises(self, engine):
        with pytest.raises(ValueError, match="invalid dependencies"):
            engine._validate_dependencies(2, [-1])

    def test_empty_dependencies(self, engine):
        engine._validate_dependencies(5, [])  # Should not raise


# ---------------------------------------------------------------------------
# Skip reason logic
# ---------------------------------------------------------------------------

class TestGetSkipReason:
    def test_no_dependencies(self, engine):
        assert engine._get_skip_reason([], []) is None

    def test_failed_dependency(self, engine):
        results = [{"action": "tool", "status": "failed"}]
        reason = engine._get_skip_reason([0], results)
        assert reason is not None
        assert "failed" in reason

    def test_skipped_dependency(self, engine):
        results = [{"action": "tool", "status": "skipped"}]
        reason = engine._get_skip_reason([0], results)
        assert reason is not None
        assert "skipped" in reason

    def test_condition_not_passed(self, engine):
        results = [{"action": "condition", "status": "completed", "result": {"passed": False}}]
        reason = engine._get_skip_reason([0], results)
        assert reason is not None
        assert "Condition" in reason

    def test_condition_passed(self, engine):
        results = [{"action": "condition", "status": "completed", "result": {"passed": True}}]
        reason = engine._get_skip_reason([0], results)
        assert reason is None

    def test_successful_dependency(self, engine):
        results = [{"action": "tool", "status": "completed"}]
        reason = engine._get_skip_reason([0], results)
        assert reason is None


# ---------------------------------------------------------------------------
# Parameter rendering / interpolation
# ---------------------------------------------------------------------------

class TestRenderParams:
    def test_simple_string(self, engine):
        result = engine._render_params("hello", [], {})
        assert result == "hello"

    def test_step_interpolation(self, engine):
        results = [{"result": {"output": "value1"}}]
        result = engine._render_params("Step output: {{steps.0.output}}", results, {})
        assert result == "Step output: value1"

    def test_workflow_interpolation(self, engine):
        context = {"run_id": "abc-123", "workflow_name": "test"}
        result = engine._render_params("Run: {{workflow.run_id}}", [], context)
        assert result == "Run: abc-123"

    def test_nested_dict(self, engine):
        results = [{"result": {"data": "x"}}]
        params = {"key": "{{steps.0.data}}", "nested": {"inner": "{{steps.0.data}}"}}
        result = engine._render_params(params, results, {})
        assert result["key"] == "x"
        assert result["nested"]["inner"] == "x"

    def test_list_params(self, engine):
        results = [{"result": {"val": "a"}}]
        params = ["{{steps.0.val}}", "literal"]
        result = engine._render_params(params, results, {})
        assert result == ["a", "literal"]

    def test_non_string_passthrough(self, engine):
        assert engine._render_params(42, [], {}) == 42
        assert engine._render_params(True, [], {}) is True
        assert engine._render_params(None, [], {}) is None

    def test_missing_step_result(self, engine):
        results = [{"result": {"a": "b"}}]
        result = engine._render_params("{{steps.0.missing}}", results, {})
        assert result == ""

    def test_missing_workflow_context(self, engine):
        result = engine._render_params("{{workflow.missing}}", [], {})
        assert result == ""


# ---------------------------------------------------------------------------
# Result lookup
# ---------------------------------------------------------------------------

class TestLookupResult:
    def test_no_path(self, engine):
        results = [{"result": {"key": "value"}}]
        assert engine._lookup_result(results, 0, None) == {"key": "value"}

    def test_simple_path(self, engine):
        results = [{"result": {"key": "value"}}]
        assert engine._lookup_result(results, 0, "key") == "value"

    def test_nested_path(self, engine):
        results = [{"result": {"outer": {"inner": 42}}}]
        assert engine._lookup_result(results, 0, "outer.inner") == 42

    def test_missing_path(self, engine):
        results = [{"result": {"key": "value"}}]
        assert engine._lookup_result(results, 0, "nonexistent") is None

    def test_path_on_non_dict(self, engine):
        results = [{"result": "string_value"}]
        assert engine._lookup_result(results, 0, "key") is None
