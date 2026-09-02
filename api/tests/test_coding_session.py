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
from aria.agents.backends.registry import BackendRegistry
from aria.agents.backends.registry import CodingBackendUnavailableError


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
        mock_settings.coding_default_host = ""
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
        mgr._preflight_local_backend = AsyncMock()

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
# send_input on a pi-code session routes through the generic shell path
# (the real Pi executable runs in tmux like every other backend). It is plain
# tmux send-keys, not a new ARIA orchestrator turn, so it
# does NOT re-check the killswitch/e-stop per call (those gate start_session
# and the Ralph loop's per-nudge check, same as claude_code/codex/pool).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_input_pi_code_routes_through_shell_substrate():
    """A pi-code session with a shell_name sends input the same way any other
    shell-substrate backend does -- no special-casing, no per-call safety gate."""
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(return_value={
        "_id": "sess-pi",
        "status": "running",
        "backend": "pi-code",
        "shell_name": "claude-coding-sess-pi",
    })
    mgr = _make_manager(db=db)
    mgr.shell_service = MagicMock()
    mgr.shell_service.send_input = AsyncMock(return_value=(5, "screen text"))

    result = await mgr.send_input("sess-pi", "keep going")

    assert result is True
    mgr.shell_service.send_input.assert_awaited_once_with("claude-coding-sess-pi", "keep going")


@pytest.mark.asyncio
async def test_start_session_subagent_profile_llm_backend_not_confused_with_session_backend():
    """A specialist profile's llm.backend is a Pi provider name (aria) — a
    different vocabulary from the coding-session
    substrate (claude_code/codex/pi-code/pool). It must be routed through
    the external pi-code process with that name pinned as its provider, not
    adopted as the session `backend` itself.

    The historical pi-coding-ridge slug now selects Flash Next through ARIA;
    it must not be interpreted as a coding-session process backend.
    """
    db = make_mock_db()

    async def fake_agents_find_one(query):
        if query.get("slug") == "pi-coding-ridge":
            return {
                "slug": "pi-coding-ridge",
                "llm": {
                    "backend": "aria",
                    "model": "Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K",
                },
                "system_prompt": "Use Flash Next through ARIA.",
            }
        return None

    db.agents.find_one = AsyncMock(side_effect=fake_agents_find_one)

    mgr = _make_manager(db=db)
    mgr.registry = BackendRegistry()  # real registry: only it can tell "ridge" apart from "pi-code"

    with patch("aria.api.deps.get_killswitch") as mock_get_ks, \
         patch("aria.api.deps.resolve_estop_manager", new_callable=AsyncMock) as mock_resolve_estop, \
         patch.object(mgr, "_launch_substrate", new_callable=AsyncMock) as mock_launch:
        mock_get_ks.return_value.check_or_raise = MagicMock()
        mock_estop = MagicMock()
        mock_estop.is_active = AsyncMock(return_value=False)
        mock_resolve_estop.return_value = mock_estop
        mock_launch.return_value = {"_id": "sess-ridge", "status": "running"}

        await mgr.start_session(
            workspace="/tmp/ws",
            backend=None,
            prompt="fix the bug",
            subagent_profile="pi-coding-ridge",
        )

    mock_launch.assert_awaited_once()
    command = mock_launch.call_args.args[1]
    assert command.argv[1:5] == [
        "--provider", "aria", "--model", "Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K",
    ]
    inserted = db.coding_sessions.insert_one.call_args.args[0]
    assert inserted["backend"] == "pi-code"
    assert inserted["llm"] == "aria"
    assert "agent_conversation_id" not in inserted


@pytest.mark.asyncio
async def test_start_session_refuses_pool_when_disabled():
    """settings.pool_enabled=False must refuse a pool-backed session up front
    with a clear error, not attempt to dial the (physically shut down)
    chadrock server and fail with a confusing connection error instead."""
    mgr = _make_manager()
    mgr.registry = BackendRegistry()  # real registry: need "pool" to canonicalize

    with patch("aria.api.deps.get_killswitch") as mock_get_ks, \
         patch("aria.api.deps.resolve_estop_manager", new_callable=AsyncMock) as mock_resolve_estop, \
         patch("aria.agents.session.settings.pool_enabled", False), \
         patch("aria.agents.session.settings.coding_routing_enabled", False):
        mock_get_ks.return_value.check_or_raise = MagicMock()
        mock_estop = MagicMock()
        mock_estop.is_active = AsyncMock(return_value=False)
        mock_resolve_estop.return_value = mock_estop

        with pytest.raises(RuntimeError, match="pool backend is disabled"):
            await mgr.start_session(workspace="/tmp/ws", backend="pool", prompt="do stuff")


@pytest.mark.asyncio
async def test_backend_preflight_reports_missing_binary_structurally():
    mgr = _make_manager()
    real_preflight = type(mgr)._preflight_local_backend
    with patch("aria.agents.session.settings.codex_binary", "missing-codex"), patch(
        "aria.agents.session.shutil.which", return_value=None
    ):
        with pytest.raises(CodingBackendUnavailableError) as error:
            await real_preflight(mgr, "codex")
    assert error.value.backend == "codex"
    assert error.value.retryable is False
    assert "executable not found" in error.value.reason


@pytest.mark.asyncio
async def test_send_input_pi_code_without_shell_falls_back_to_process_manager():
    """A pi-code session that somehow has no shell_name (e.g. shell-substrate
    spawn failed and the session errored before one was assigned) falls back
    to the generic process-manager path, same as any other backend without
    a shell -- not a crash, not a special pi-code branch."""
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(return_value={
        "_id": "sess-pi",
        "status": "running",
        "backend": "pi-code",
        "shell_name": None,
    })
    mgr = _make_manager(db=db)

    result = await mgr.send_input("sess-pi", "keep going")

    assert result is True  # _make_manager's mock_proc_mgr.send_input returns True
    mgr.process_manager.send_input.assert_awaited_once_with("sess-pi", "keep going")


class TestQuotaCooldownNoFallback:
    """With no fallback backend configured (the 2026-07-30 default), an
    exhausted Claude quota must FAIL AND PAUSE, not silently downgrade."""

    @pytest.mark.asyncio
    async def test_cooldown_raises_when_no_fallback_configured(self):
        from datetime import datetime, timedelta, timezone
        from aria.agents.routing import (
            ComplexityRouter,
            QuotaCooldownError,
            RoutingVerdict,
        )

        router = ComplexityRouter(db=MagicMock())
        verdict = RoutingVerdict(
            tier="standard", backend="claude_code", model="claude-sonnet-5",
            llm="", why="", confidence=1.0, source="heuristic", judge_model=None,
        )
        cooled = datetime.now(timezone.utc) + timedelta(minutes=30)
        with patch("aria.agents.routing.get_cooldown", AsyncMock(return_value=cooled)), \
             patch("aria.agents.routing.settings.coding_routing_fallback_backend", ""):
            with pytest.raises(QuotaCooldownError, match="no fallback"):
                await router._apply_availability(verdict)

    @pytest.mark.asyncio
    async def test_cooldown_still_demotes_when_fallback_is_configured(self):
        """Opt-in path: setting a backend restores the old demotion behaviour."""
        from datetime import datetime, timedelta, timezone
        from aria.agents.routing import ComplexityRouter, RoutingVerdict

        router = ComplexityRouter(db=MagicMock())
        verdict = RoutingVerdict(
            tier="standard", backend="claude_code", model="claude-sonnet-5",
            llm="", why="", confidence=1.0, source="heuristic", judge_model=None,
        )
        cooled = datetime.now(timezone.utc) + timedelta(minutes=30)
        with patch("aria.agents.routing.get_cooldown", AsyncMock(return_value=cooled)), \
             patch("aria.agents.routing.settings.coding_routing_fallback_backend", "pi-code"), \
             patch("aria.agents.routing.settings.coding_routing_fallback_model", "x"), \
             patch("aria.agents.routing.settings.coding_routing_fallback_llm", ""):
            out = await router._apply_availability(verdict)
        assert out.backend == "pi-code"
        assert out.source == "fallback"
