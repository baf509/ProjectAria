"""Tests for the autopilot system: AutopilotPlanner, AutopilotExecutor, AutopilotService."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_mock_db, FakeLLMAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_autopilot_db():
    """Extend make_mock_db with autopilot-specific collections."""
    db = make_mock_db()

    sess_coll = MagicMock()
    sess_coll.find_one = AsyncMock(return_value=None)
    sess_coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="mock-id"))
    sess_coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    sess_cursor = MagicMock()
    sess_cursor.sort = MagicMock(return_value=sess_cursor)
    sess_cursor.limit = MagicMock(return_value=sess_cursor)
    sess_cursor.to_list = AsyncMock(return_value=[])
    sess_coll.find = MagicMock(return_value=sess_cursor)
    db.autopilot_sessions = sess_coll

    return db


def _make_steps(n=3):
    """Generate a list of plan steps."""
    return [
        {
            "index": i,
            "name": f"Step {i+1}",
            "action": "llm_query",
            "description": f"Do thing {i+1}",
            "tool_name": None,
            "tool_arguments": None,
            "depends_on": [],
            "status": "pending",
            "result": None,
        }
        for i in range(n)
    ]


# ===================================================================
# AutopilotPlanner tests
# ===================================================================

class TestAutopilotPlanner:

    @pytest.mark.asyncio
    async def test_create_plan_parses_json(self):
        """Parses JSON array from LLM response."""
        from aria.autopilot.planner import AutopilotPlanner

        json_response = '[{"name": "Research", "action": "llm_query", "description": "Look it up"}]'
        fake_adapter = FakeLLMAdapter(response_text=json_response)

        planner = AutopilotPlanner()
        with patch("aria.autopilot.planner.load_prompt", return_value="plan this"), \
             patch("aria.autopilot.planner.ClaudeRunner") as mock_cr, \
             patch("aria.autopilot.planner.llm_manager") as mock_mgr:
            mock_cr.is_available.return_value = False
            mock_mgr.get_adapter.return_value = fake_adapter

            steps = await planner.create_plan("Build a widget")

        assert len(steps) == 1
        assert steps[0]["name"] == "Research"
        assert steps[0]["index"] == 0
        assert steps[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_create_plan_fallback_single_step(self):
        """Unparseable response returns single step."""
        from aria.autopilot.planner import AutopilotPlanner

        fake_adapter = FakeLLMAdapter(response_text="I can't produce JSON right now, sorry.")

        planner = AutopilotPlanner()
        with patch("aria.autopilot.planner.load_prompt", return_value="plan this"), \
             patch("aria.autopilot.planner.ClaudeRunner") as mock_cr, \
             patch("aria.autopilot.planner.llm_manager") as mock_mgr:
            mock_cr.is_available.return_value = False
            mock_mgr.get_adapter.return_value = fake_adapter

            steps = await planner.create_plan("Build a widget")

        assert len(steps) == 1
        assert steps[0]["name"] == "Execute goal"
        assert steps[0]["description"] == "Build a widget"

    @pytest.mark.asyncio
    async def test_create_plan_normalizes_steps(self):
        """Adds index, defaults for missing fields."""
        from aria.autopilot.planner import AutopilotPlanner

        json_response = '[{"name": "A"}, {"name": "B", "action": "tool_call"}]'
        fake_adapter = FakeLLMAdapter(response_text=json_response)

        planner = AutopilotPlanner()
        with patch("aria.autopilot.planner.load_prompt", return_value="plan this"), \
             patch("aria.autopilot.planner.ClaudeRunner") as mock_cr, \
             patch("aria.autopilot.planner.llm_manager") as mock_mgr:
            mock_cr.is_available.return_value = False
            mock_mgr.get_adapter.return_value = fake_adapter

            steps = await planner.create_plan("Do stuff")

        assert steps[0]["index"] == 0
        assert steps[1]["index"] == 1
        assert steps[0]["action"] == "llm_query"  # default
        assert steps[1]["action"] == "tool_call"   # preserved
        assert steps[0]["depends_on"] == []
        assert steps[0]["status"] == "pending"
        assert steps[0]["result"] is None


# ===================================================================
# AutopilotExecutor tests
# ===================================================================

class TestAutopilotExecutor:

    def _make_executor(self, db=None, killswitch=None):
        from aria.autopilot.executor import AutopilotExecutor

        db = db or _make_autopilot_db()
        ks = killswitch or MagicMock()
        ks.check_or_raise = MagicMock()
        return AutopilotExecutor(db=db, killswitch=ks)

    @pytest.mark.asyncio
    async def test_execute_plan_completes_steps(self):
        """Executes all steps successfully."""
        executor = self._make_executor()

        with patch.object(executor, "_execute_step", new_callable=AsyncMock, return_value="done"):
            results = await executor.execute_plan(
                session_id="sess-1",
                steps=_make_steps(2),
                mode="unrestricted",
            )

        assert len(results) == 2
        assert all(r["status"] == "completed" for r in results)

    @pytest.mark.asyncio
    async def test_execute_plan_step_fails_stops(self):
        """Stops on first failure."""
        executor = self._make_executor()

        call_count = 0

        async def _fail_second(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("step failed")
            return "ok"

        with patch.object(executor, "_execute_step", side_effect=_fail_second):
            results = await executor.execute_plan(
                session_id="sess-1",
                steps=_make_steps(3),
                mode="unrestricted",
            )

        assert len(results) == 2
        assert results[0]["status"] == "completed"
        assert results[1]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_execute_plan_killswitch_blocks(self):
        """Killswitch raises stops execution."""
        from aria.autopilot.executor import AutopilotExecutor

        db = _make_autopilot_db()
        ks = MagicMock()
        ks.check_or_raise = MagicMock(side_effect=RuntimeError("Killswitch engaged"))
        executor = AutopilotExecutor(db=db, killswitch=ks)

        with pytest.raises(RuntimeError, match="Killswitch engaged"):
            await executor.execute_plan(
                session_id="sess-1",
                steps=_make_steps(1),
                mode="unrestricted",
            )

    def test_approve_step_sets_gate(self):
        """Approval gate gets set."""
        executor = self._make_executor()

        gate = asyncio.Event()
        executor._approval_gates["sess-1"] = {0: gate}

        result = executor.approve_step("sess-1", 0)

        assert result is True
        assert gate.is_set()

    def test_approve_step_not_found(self):
        """Returns False for unknown step."""
        executor = self._make_executor()

        result = executor.approve_step("nonexistent", 99)
        assert result is False

    def test_cancel_session_unblocks_gates(self):
        """All gates set on cancel."""
        executor = self._make_executor()

        gate0 = asyncio.Event()
        gate1 = asyncio.Event()
        executor._approval_gates["sess-1"] = {0: gate0, 1: gate1}

        executor.cancel_session("sess-1")

        assert gate0.is_set()
        assert gate1.is_set()
        assert "sess-1" not in executor._approval_gates


# ===================================================================
# AutopilotService tests
# ===================================================================

class TestAutopilotService:

    def _make_service(self, db=None, killswitch=None, task_runner=None):
        from aria.autopilot.service import AutopilotService

        db = db or _make_autopilot_db()
        ks = killswitch or MagicMock()
        ks.check_or_raise = MagicMock()
        tr = task_runner or MagicMock()
        tr.submit_task = AsyncMock(return_value="task-123")
        tr.cancel_task = AsyncMock()
        return AutopilotService(db=db, killswitch=ks, task_runner=tr)

    @pytest.mark.asyncio
    async def test_start_creates_session(self):
        """Creates plan, persists to DB, submits task."""
        db = _make_autopilot_db()
        svc = self._make_service(db=db)

        fake_steps = _make_steps(2)
        with patch.object(svc.planner, "create_plan", new_callable=AsyncMock, return_value=fake_steps):
            result = await svc.start(goal="Deploy app", mode="safe")

        assert "session_id" in result
        assert result["task_id"] == "task-123"
        assert result["goal"] == "Deploy app"
        assert result["mode"] == "safe"
        assert result["step_count"] == 2
        db.autopilot_sessions.insert_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_invalid_mode(self):
        """Raises ValueError for bad mode."""
        svc = self._make_service()

        with pytest.raises(ValueError, match="Mode must be"):
            await svc.start(goal="Do stuff", mode="yolo")

    @pytest.mark.asyncio
    async def test_stop_session(self):
        """Cancels task, updates DB."""
        db = _make_autopilot_db()
        db.autopilot_sessions.find_one = AsyncMock(return_value={
            "_id": "sess-1",
            "goal": "Deploy",
            "mode": "safe",
            "status": "running",
            "task_id": "task-123",
            "steps": _make_steps(1),
        })
        task_runner = MagicMock()
        task_runner.submit_task = AsyncMock(return_value="task-123")
        task_runner.cancel_task = AsyncMock()

        svc = self._make_service(db=db, task_runner=task_runner)

        result = await svc.stop("sess-1")

        assert result["status"] == "stopped"
        task_runner.cancel_task.assert_awaited_once_with("task-123")
        db.autopilot_sessions.update_one.assert_awaited()

    @pytest.mark.asyncio
    async def test_get_session_not_found(self):
        """Returns None for missing session."""
        db = _make_autopilot_db()
        db.autopilot_sessions.find_one = AsyncMock(return_value=None)
        svc = self._make_service(db=db)

        result = await svc.get_session("nonexistent")
        assert result is None
