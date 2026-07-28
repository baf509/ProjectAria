"""
ARIA - Tests for CodingSessionManager

Tests for session lifecycle: start, stop, get, list, output, resume.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tests.conftest import make_mock_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(db=None, tmux=None, notification_service=None):
    """Build a CodingSessionManager with mocked internals."""
    with patch("aria.agents.session.TmuxManager") as TmuxCls, \
         patch("aria.agents.session.BackendRegistry") as RegCls, \
         patch("aria.agents.session.CodingSubprocessManager") as ProcCls, \
         patch("aria.agents.session.AgentMailbox") as MailCls, \
         patch("aria.agents.session.settings") as mock_settings:

        mock_settings.coding_default_backend = "claude-code"
        mock_settings.coding_output_lines = 500

        # Prevent real tmux availability check
        TmuxCls.is_available.return_value = tmux is not None

        from aria.agents.session import CodingSessionManager

        mgr = CodingSessionManager(db or make_mock_db(), notification_service=notification_service)

        # Replace internals with controllable mocks
        mock_backend = MagicMock()
        mock_backend.start_command.return_value = MagicMock(
            argv=["claude", "--prompt", "do stuff"],
            cwd="/tmp/workspace",
            env=None,
        )
        mgr.registry = MagicMock()
        mgr.registry.get.return_value = mock_backend

        mock_proc_mgr = MagicMock()
        running = MagicMock()
        running.process.pid = 12345
        mock_proc_mgr.spawn = AsyncMock(return_value=running)
        mock_proc_mgr.stop = AsyncMock(return_value=True)
        mock_proc_mgr.get_output = MagicMock(return_value="some output")
        mock_proc_mgr.wait = AsyncMock(return_value=0)
        mock_proc_mgr.send_input = AsyncMock(return_value=True)
        mgr.process_manager = mock_proc_mgr

        if tmux is not None:
            mgr.tmux_manager = tmux
        else:
            mgr.tmux_manager = None

        mgr.mailbox = MagicMock()
        mgr.mailbox.send_task_done = AsyncMock()

        return mgr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_session_basic():
    """start_session spawns a process, inserts DB doc, and returns session dict."""
    db = make_mock_db()
    session_doc = {
        "_id": "test-id",
        "status": "running",
        "pid": 12345,
        "workspace": "/tmp/workspace",
        "backend": "claude-code",
    }
    db.coding_sessions.find_one = AsyncMock(return_value=session_doc)

    mgr = _make_manager(db=db)

    result = await mgr.start_session(
        workspace="/tmp/workspace",
        backend="claude-code",
        prompt="implement feature X",
    )

    # DB insert called
    db.coding_sessions.insert_one.assert_awaited_once()
    # Process spawned
    mgr.process_manager.spawn.assert_awaited_once()
    # DB updated with pid
    assert db.coding_sessions.update_one.await_count >= 1
    # Returns session dict
    assert result["status"] == "running"
    assert result["pid"] == 12345


@pytest.mark.asyncio
async def test_start_session_visible_tmux():
    """When visible=True and tmux is available, spawns in tmux pane."""
    db = make_mock_db()
    session_doc = {
        "_id": "tmux-sess",
        "status": "running",
        "tmux_pane_id": "%42",
        "workspace": "/tmp/workspace",
    }
    db.coding_sessions.find_one = AsyncMock(return_value=session_doc)

    mock_tmux = MagicMock()
    pane = MagicMock()
    pane.pane_id = "%42"
    mock_tmux.spawn_pane = AsyncMock(return_value=pane)

    mgr = _make_manager(db=db, tmux=mock_tmux)

    result = await mgr.start_session(
        workspace="/tmp/workspace",
        backend="claude-code",
        prompt="fix bug",
        visible=True,
    )

    mock_tmux.spawn_pane.assert_awaited_once()
    # Process manager should NOT have been called
    mgr.process_manager.spawn.assert_not_awaited()
    assert result["tmux_pane_id"] == "%42"


@pytest.mark.asyncio
async def test_stop_session_success():
    """stop_session stops the process and updates DB status to 'stopped'."""
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(return_value={
        "_id": "sess-1",
        "status": "running",
        "tmux_pane_id": None,
        "workspace": "/tmp/w",
    })
    mgr = _make_manager(db=db)

    result = await mgr.stop_session("sess-1")

    assert result is True
    mgr.process_manager.stop.assert_awaited_once_with("sess-1")
    # Check DB was updated
    update_call = db.coding_sessions.update_one.call_args_list[-1]
    assert update_call[0][0] == {"_id": "sess-1"}
    assert update_call[0][1]["$set"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_stop_session_not_found():
    """stop_session returns False when session doesn't exist."""
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(return_value=None)
    mgr = _make_manager(db=db)

    result = await mgr.stop_session("nonexistent")

    assert result is False


@pytest.mark.asyncio
async def test_get_session():
    """get_session returns the DB document."""
    db = make_mock_db()
    expected = {"_id": "s1", "status": "running", "workspace": "/tmp"}
    db.coding_sessions.find_one = AsyncMock(return_value=expected)
    mgr = _make_manager(db=db)

    result = await mgr.get_session("s1")

    assert result == expected
    db.coding_sessions.find_one.assert_awaited_once_with({"_id": "s1"})


@pytest.mark.asyncio
async def test_list_sessions():
    """list_sessions queries DB with optional status filter."""
    db = make_mock_db()
    docs = [
        {"_id": "s1", "status": "running"},
        {"_id": "s2", "status": "running"},
    ]
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=docs)
    db.coding_sessions.find = MagicMock(return_value=cursor)
    mgr = _make_manager(db=db)

    # With status filter
    result = await mgr.list_sessions(status="running")

    db.coding_sessions.find.assert_called_once_with({"status": "running"})
    assert len(result) == 2

    # Without status filter
    db.coding_sessions.find.reset_mock()
    cursor.to_list = AsyncMock(return_value=docs)
    db.coding_sessions.find = MagicMock(return_value=cursor)

    result = await mgr.list_sessions()
    db.coding_sessions.find.assert_called_once_with({})


@pytest.mark.asyncio
async def test_get_output():
    """get_output returns process manager output for non-tmux sessions."""
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(return_value={
        "_id": "s1",
        "status": "running",
        "tmux_pane_id": None,
    })
    mgr = _make_manager(db=db)
    mgr.process_manager.get_output.return_value = "line1\nline2\nline3"

    result = await mgr.get_output("s1", lines=50)

    assert result == "line1\nline2\nline3"
    mgr.process_manager.get_output.assert_called_once_with("s1", lines=50)


@pytest.mark.asyncio
async def test_resume_session_no_checkpoint():
    """resume_session returns None when no checkpoint is found."""
    db = make_mock_db()
    mgr = _make_manager(db=db)

    with patch("aria.agents.session.find_resumable_checkpoint", new_callable=AsyncMock) as mock_find:
        mock_find.return_value = None

        result = await mgr.resume_session(workspace="/tmp/workspace")

    assert result is None


@pytest.mark.asyncio
async def test_resume_session_found():
    """resume_session finds checkpoint and starts a new session with resume prompt."""
    db = make_mock_db()

    # Session doc returned by get_session for original session
    original_doc = {
        "_id": "old-sess",
        "prompt": "implement auth",
        "backend": "claude-code",
        "model": "sonnet",
        "conversation_id": "conv-1",
        "status": "failed",
    }
    # After start_session creates new doc, get_session returns the new one
    new_doc = {
        "_id": "new-sess",
        "status": "running",
        "workspace": "/tmp/workspace",
    }
    db.coding_sessions.find_one = AsyncMock(side_effect=[original_doc, new_doc, new_doc])

    mgr = _make_manager(db=db)

    mock_checkpoint = MagicMock()
    mock_checkpoint.session_id = "old-sess"
    mock_checkpoint.branch = "feature/auth"
    mock_checkpoint.last_commit = "abc123"
    mock_checkpoint.notes = "Session exited with code 1"

    with patch("aria.agents.session.find_resumable_checkpoint", new_callable=AsyncMock) as mock_find, \
         patch("aria.agents.session.build_resume_prompt") as mock_build:
        mock_find.return_value = mock_checkpoint
        mock_build.return_value = "Resume: implement auth (from checkpoint)"

        result = await mgr.resume_session(workspace="/tmp/workspace")

    assert result is not None
    mock_build.assert_called_once_with(mock_checkpoint, "implement auth")
    # Process should have been spawned for the new session
    mgr.process_manager.spawn.assert_awaited_once()


# ---------------------------------------------------------------------------
# _watch_session — pool's exit code 4 is a real result, not a crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watch_session_pool_expected_failure_skips_checkpoint():
    """Exit code 4 from the pool backend is a completed-but-unsuccessful task,
    not a crash -- _watch_session must not write a crash-recovery checkpoint
    for it (see backends/pool.py's is_expected_failure_exit_code)."""
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(return_value={
        "_id": "sess-pool",
        "status": "running",
        "workspace": "/tmp/w",
        "backend": "pool",
        "conversation_id": None,
    })
    mgr = _make_manager(db=db)
    mgr.process_manager.wait = AsyncMock(return_value=4)

    pool_backend = MagicMock()
    pool_backend.is_expected_failure_exit_code = MagicMock(return_value=True)
    mgr.registry.get.return_value = pool_backend

    with patch("aria.agents.session.write_checkpoint", new_callable=AsyncMock) as mock_checkpoint:
        await mgr._watch_session("sess-pool")

    mock_checkpoint.assert_not_awaited()
    update_call = db.coding_sessions.update_one.call_args_list[-1]
    assert update_call[0][1]["$set"]["status"] == "failed"
    assert update_call[0][1]["$set"]["exit_code"] == 4


@pytest.mark.asyncio
async def test_watch_session_real_crash_still_writes_checkpoint():
    """A genuine crash (exit code the backend doesn't recognize as expected)
    must still get a crash-recovery checkpoint -- no regression from the
    pool-specific carve-out above."""
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(return_value={
        "_id": "sess-crash",
        "status": "running",
        "workspace": "/tmp/w",
        "backend": "claude_code",
        "conversation_id": None,
    })
    mgr = _make_manager(db=db)
    mgr.process_manager.wait = AsyncMock(return_value=1)

    crash_backend = MagicMock()
    crash_backend.is_expected_failure_exit_code = MagicMock(return_value=False)
    mgr.registry.get.return_value = crash_backend

    with patch("aria.agents.session.write_checkpoint", new_callable=AsyncMock) as mock_checkpoint:
        await mgr._watch_session("sess-crash")

    mock_checkpoint.assert_awaited_once()
    update_call = db.coding_sessions.update_one.call_args_list[-1]
    assert update_call[0][1]["$set"]["status"] == "failed"


# ---------------------------------------------------------------------------
# send_input on a pi-code session must honor the same fail-closed safety
# gate as start_session / the deferred-launch re-check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_input_pi_code_blocked_by_estop():
    """An active emergency stop must block a pi-code follow-up turn, not just
    the initial spawn."""
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(return_value={
        "_id": "sess-pi",
        "status": "running",
        "backend": "pi-code",
        "agent_conversation_id": "conv-1",
    })
    mgr = _make_manager(db=db)

    mock_estop = MagicMock()
    mock_estop.is_active = AsyncMock(return_value=True)
    mock_estop.get_state = AsyncMock(return_value=MagicMock(reason="rate limit cooldown"))

    with patch("aria.api.deps.get_killswitch") as mock_get_ks, \
         patch("aria.api.deps.resolve_estop_manager", new_callable=AsyncMock) as mock_resolve_estop:
        mock_get_ks.return_value.check_or_raise = MagicMock()  # killswitch: not engaged
        mock_resolve_estop.return_value = mock_estop

        result = await mgr.send_input("sess-pi", "keep going")

    assert result is False
    assert "sess-pi" not in mgr._watch_tasks


@pytest.mark.asyncio
async def test_send_input_pi_code_blocked_by_killswitch():
    """An engaged manual killswitch must block a pi-code follow-up turn."""
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(return_value={
        "_id": "sess-pi",
        "status": "running",
        "backend": "pi-code",
        "agent_conversation_id": "conv-1",
    })
    mgr = _make_manager(db=db)

    with patch("aria.api.deps.get_killswitch") as mock_get_ks:
        mock_get_ks.return_value.check_or_raise = MagicMock(
            side_effect=RuntimeError("killswitch engaged")
        )

        result = await mgr.send_input("sess-pi", "keep going")

    assert result is False
    assert "sess-pi" not in mgr._watch_tasks
