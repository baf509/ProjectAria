"""Tests for aria.workflows.engine — parameter rendering, conditions,
dependencies, and fan-out (parallel/map/synthesize) orchestration."""

from unittest.mock import AsyncMock

import pytest

from aria.workflows.engine import WorkflowEngine


@pytest.fixture
def engine():
    """Create a WorkflowEngine with None dependencies for pure method testing."""
    engine = WorkflowEngine.__new__(WorkflowEngine)
    return engine


_WF = {"name": "t", "_id": "w", "_active_run_id": "r"}


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

    def test_list_index_in_path(self, engine):
        # Fan-out group result: {{steps.0.results.1.result_summary}}
        results = [{"result": {"results": [
            {"result_summary": "first"}, {"result_summary": "second"},
        ]}}]
        assert engine._lookup_result(results, 0, "results.1.result_summary") == "second"

    def test_list_index_out_of_range(self, engine):
        results = [{"result": {"results": [1, 2]}}]
        assert engine._lookup_result(results, 0, "results.9") is None


# ---------------------------------------------------------------------------
# List coercion + stringify helpers (map `over`, synthesize inputs)
# ---------------------------------------------------------------------------

class TestCoerceList:
    def test_passthrough_list(self, engine):
        assert engine._coerce_list([1, 2, 3]) == [1, 2, 3]

    def test_json_array_string(self, engine):
        assert engine._coerce_list('["a", "b"]') == ["a", "b"]

    def test_newline_string(self, engine):
        assert engine._coerce_list("a\nb\n\nc") == ["a", "b", "c"]

    def test_csv_string(self, engine):
        assert engine._coerce_list("a, b ,c") == ["a", "b", "c"]

    def test_empty_and_none(self, engine):
        assert engine._coerce_list("") == []
        assert engine._coerce_list(None) == []

    def test_scalar_wraps(self, engine):
        assert engine._coerce_list(42) == [42]


class TestStringify:
    def test_none_empty(self, engine):
        assert engine._stringify(None) == ""

    def test_str_passthrough(self, engine):
        assert engine._stringify("hi") == "hi"

    def test_dict_json(self, engine):
        out = engine._stringify({"a": 1})
        assert '"a": 1' in out


# ---------------------------------------------------------------------------
# Scope rendering ({{item}} / {{index}}) inside a map
# ---------------------------------------------------------------------------

class TestScopeRender:
    def test_item_scalar(self, engine):
        out = engine._render_params("q={{item}}", [], {}, scope={"item": "x", "index": 0})
        assert out == "q=x"

    def test_item_path(self, engine):
        out = engine._render_params(
            "{{item.name}}", [], {}, scope={"item": {"name": "bob"}, "index": 2}
        )
        assert out == "bob"

    def test_index(self, engine):
        out = engine._render_params("i={{index}}", [], {}, scope={"item": "x", "index": 3})
        assert out == "i=3"


# ---------------------------------------------------------------------------
# Fan-out: parallel + map groups
# ---------------------------------------------------------------------------

class TestFanOut:
    @pytest.mark.asyncio
    async def test_parallel_runs_substeps(self, engine):
        step = {"action": "parallel", "params": {"steps": [
            {"action": "condition", "params": {"value": "a", "equals": "a"}},
            {"action": "condition", "params": {"value": "a", "equals": "b"}},
        ]}}
        record = await engine._execute_step(
            workflow=_WF, index=0, step=step, results=[], dry_run=False
        )
        assert record["status"] == "completed"
        res = record["result"]
        assert res["count"] == 2
        assert res["results"][0]["passed"] is True
        assert res["results"][1]["passed"] is False

    @pytest.mark.asyncio
    async def test_map_over_list_with_item(self, engine):
        step = {"action": "map", "params": {
            "over": ["a", "b", "c"],
            "template": {"action": "condition", "params": {"value": "{{item}}", "equals": "b"}},
        }}
        record = await engine._execute_step(
            workflow=_WF, index=0, step=step, results=[], dry_run=False
        )
        res = record["result"]
        assert res["count"] == 3
        passed = [r["passed"] for r in res["results"]]
        assert passed == [False, True, False]

    @pytest.mark.asyncio
    async def test_map_over_interpolated_json(self, engine):
        # `over` referencing a prior step's list result via interpolation.
        results = [{"result": {"items": '["x", "y"]'}, "action": "tool", "status": "completed"}]
        step = {"action": "map", "params": {
            "over": "{{steps.0.items}}",
            "template": {"action": "condition", "params": {"value": "{{item}}", "exists": True}},
        }}
        record = await engine._execute_step(
            workflow=_WF, index=1, step=step, results=results, dry_run=False
        )
        assert record["result"]["count"] == 2

    @pytest.mark.asyncio
    async def test_empty_map(self, engine):
        step = {"action": "map", "params": {"over": [], "template": {"action": "condition", "params": {}}}}
        record = await engine._execute_step(
            workflow=_WF, index=0, step=step, results=[], dry_run=False
        )
        assert record["result"]["count"] == 0

    @pytest.mark.asyncio
    async def test_all_substeps_failed_marks_group_failed(self, engine):
        # Every sub-step uses an unsupported action, so every one fails.
        step = {"action": "parallel", "params": {"steps": [
            {"action": "bogus"},
            {"action": "bogus"},
        ]}}
        record = await engine._execute_step(
            workflow=_WF, index=0, step=step, results=[], dry_run=False
        )
        assert record["status"] == "failed"
        assert record["result"]["failed"] == 2

    @pytest.mark.asyncio
    async def test_partial_substep_failure_still_completed(self, engine):
        # Only some sub-steps fail — existing best-effort semantics unchanged.
        step = {"action": "parallel", "params": {"steps": [
            {"action": "condition", "params": {"value": "a", "equals": "a"}},
            {"action": "bogus"},
        ]}}
        record = await engine._execute_step(
            workflow=_WF, index=0, step=step, results=[], dry_run=False
        )
        assert record["status"] == "completed"
        assert record["result"]["failed"] == 1


# ---------------------------------------------------------------------------
# code_session await + synthesize
# ---------------------------------------------------------------------------

class TestPerformActionExtras:
    @pytest.mark.asyncio
    async def test_code_session_await_joins(self, engine):
        engine.coding_manager = AsyncMock()
        engine.coding_manager.start_session = AsyncMock(return_value={
            "_id": "s1", "workspace": "/w", "backend": "claude_code", "status": "running",
        })
        engine.coding_manager.wait_for_session = AsyncMock(return_value={
            "status": "completed", "result_summary": "did the thing",
        })
        out = await engine._perform_action(
            _WF, "code_session",
            {"workspace": "/w", "prompt": "go", "await": True}, [],
        )
        assert out["session_id"] == "s1"
        assert out["status"] == "completed"
        assert out["result_summary"] == "did the thing"
        engine.coding_manager.wait_for_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_code_session_fire_and_forget(self, engine):
        engine.coding_manager = AsyncMock()
        engine.coding_manager.start_session = AsyncMock(return_value={
            "_id": "s2", "workspace": "/w", "backend": "codex", "status": "queued",
        })
        engine.coding_manager.wait_for_session = AsyncMock()
        out = await engine._perform_action(
            _WF, "code_session", {"workspace": "/w", "prompt": "go"}, [],
        )
        assert out["session_id"] == "s2"
        assert "result_summary" not in out
        engine.coding_manager.wait_for_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_synthesize_from_steps(self, engine):
        captured = {}

        async def _fake_prompt(workflow, params):
            captured["message"] = params["message"]
            return {"conversation_id": "c1", "response": "merged"}

        engine._run_prompt_action = _fake_prompt
        results = [
            {"result": {"result_summary": "review A"}, "action": "code_session", "status": "completed"},
            {"result": {"result_summary": "review B"}, "action": "code_session", "status": "completed"},
        ]
        out = await engine._perform_action(
            _WF, "synthesize",
            {"from_steps": [0, 1], "instruction": "Reconcile these reviews."},
            results,
        )
        assert out["response"] == "merged"
        assert "Reconcile these reviews." in captured["message"]
        assert "review A" in captured["message"]
        assert "review B" in captured["message"]

    @pytest.mark.asyncio
    async def test_unknown_action_raises(self, engine):
        with pytest.raises(ValueError, match="Unsupported workflow action"):
            await engine._perform_action(_WF, "bogus", {}, [])


# ---------------------------------------------------------------------------
# Crash recovery — must resume, not replay, already-completed steps
# ---------------------------------------------------------------------------

class TestRecoverRun:
    @pytest.mark.asyncio
    async def test_recover_resumes_without_replaying_completed_steps(self, engine):
        workflow = {"name": "t", "_id": "w", "steps": [
            {"action": "tool"},
            {"action": "tool"},
        ]}
        run_doc = {
            "_id": "r1",
            "workflow_id": "w",
            "dry_run": False,
            "task_id": "pending",
            "step_results": [
                {"index": 0, "action": "tool", "depends_on": [], "status": "completed", "result": {"done": True}},
            ],
        }
        engine.db = AsyncMock()
        engine.db.workflow_runs.find_one = AsyncMock(return_value=run_doc)
        engine.db.workflow_runs.update_one = AsyncMock()
        engine.db.workflows.find_one = AsyncMock(return_value=workflow)

        perform_calls = []

        async def fake_perform(wf, action, params, results):
            perform_calls.append(action)
            return {"ok": True}

        engine._perform_action = fake_perform

        out = await engine._recover_run({"workflow_run_id": "r1", "workflow_id": "w"})

        # Only the not-yet-completed step (index 1) is re-run.
        assert perform_calls == ["tool"]
        assert len(out["step_results"]) == 2
        assert out["step_results"][0]["result"] == {"done": True}
        assert out["step_results"][1]["result"] == {"ok": True}
