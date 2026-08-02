"""
ARIA - Tests for the multi-machine node subsystem

Covers the command queue (enqueue/await/claim/complete), NodeService (registry +
host-stamped ingest), and ShellService's host-aware dispatch (send_input /
current_screen / session_alive / kill routing local vs remote) + remote coding
session routing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.config import settings
from aria.nodes import commands, is_remote_host, local_node_id
from aria.nodes.models import EventBatchIn, NodeRegisterRequest, ShellEventIn, SnapshotIn
from aria.nodes.service import NodeService
from aria.shells.service import ShellService
from tests.conftest import make_mock_db


def _fresh(seconds_ago=1):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


# --------------------------------------------------------------------- identity
def test_is_remote_host(monkeypatch):
    monkeypatch.setattr(settings, "local_node_id", "corsair")
    assert local_node_id() == "corsair"
    assert is_remote_host("bens-macbook-air") is True
    assert is_remote_host("corsair") is False
    assert is_remote_host("") is False
    assert is_remote_host(None) is False


# --------------------------------------------------------------------- commands
@pytest.mark.asyncio
async def test_enqueue_command_shape():
    db = make_mock_db()
    cid = await commands.enqueue_command(db, "mac", "send_input", {"name": "x"})
    assert isinstance(cid, str) and cid
    doc = db.shell_commands.insert_one.call_args[0][0]
    assert doc["node_id"] == "mac"
    assert doc["kind"] == "send_input"
    assert doc["status"] == "pending"
    assert doc["expires_at"] > doc["created_at"]


@pytest.mark.asyncio
async def test_await_result_returns_done():
    db = make_mock_db()
    db.shell_commands.find_one = AsyncMock(
        return_value={"_id": "c1", "status": "done", "result": {"line": 1, "screen": "hi"}}
    )
    doc = await commands.await_result(db, "c1", timeout_seconds=1)
    assert doc["status"] == "done"
    assert doc["result"]["screen"] == "hi"


@pytest.mark.asyncio
async def test_await_result_times_out():
    db = make_mock_db()
    db.shell_commands.find_one = AsyncMock(return_value={"_id": "c1", "status": "pending"})
    assert await commands.await_result(db, "c1", timeout_seconds=0) is None


@pytest.mark.asyncio
async def test_claim_commands_drains_pending():
    db = make_mock_db()
    db.shell_commands.find_one_and_update = AsyncMock(
        side_effect=[{"_id": "c1", "kind": "stop", "args": {}}, None]
    )
    claimed = await commands.claim_commands(db, "mac", poll_seconds=0)
    assert [c["_id"] for c in claimed] == ["c1"]


@pytest.mark.asyncio
async def test_claim_commands_empty_returns_fast():
    db = make_mock_db()
    db.shell_commands.find_one_and_update = AsyncMock(return_value=None)
    assert await commands.claim_commands(db, "mac", poll_seconds=0) == []


@pytest.mark.asyncio
async def test_complete_command():
    db = make_mock_db()
    ok = await commands.complete_command(db, "c1", result={"ok": True})
    assert ok is True
    update = db.shell_commands.update_one.call_args[0][1]["$set"]
    assert update["status"] == "done"
    assert update["result"] == {"ok": True}


# ------------------------------------------------------------------- NodeService
@pytest.mark.asyncio
async def test_register_upserts():
    db = make_mock_db()
    db.nodes.find_one = AsyncMock(
        return_value={"_id": "mac", "last_heartbeat_at": _fresh(), "registered_at": _fresh(60)}
    )
    svc = NodeService(db)
    await svc.register(NodeRegisterRequest(node_id="mac", hostname="bens-macbook-air"))
    args, kwargs = db.nodes.update_one.call_args
    assert args[0] == {"_id": "mac"}
    assert kwargs["upsert"] is True


@pytest.mark.asyncio
async def test_list_nodes_online_offline():
    db = make_mock_db()
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=[
        {"_id": "mac", "last_heartbeat_at": _fresh(2)},        # fresh → online
        {"_id": "old", "last_heartbeat_at": _fresh(9999)},     # stale → offline
    ])
    db.nodes.find = MagicMock(return_value=cursor)
    svc = NodeService(db)
    nodes = {n["node_id"]: n["status"] for n in await svc.list_nodes()}
    assert nodes["mac"] == "online"
    assert nodes["old"] == "offline"


@pytest.mark.asyncio
async def test_ingest_events_stamps_host():
    db = make_mock_db()
    svc = NodeService(db)
    svc.shell_service = MagicMock()
    svc.shell_service.register_shell = AsyncMock()
    svc.shell_service.insert_events_batch = AsyncMock(return_value=1)
    svc.shell_service.mark_stopped = AsyncMock()

    batch = EventBatchIn(
        shell_name="claude-x",
        project_dir="/w",
        events=[ShellEventIn(kind="output", text_raw="hello")],
    )
    n = await svc.ingest_events("mac", batch)
    assert n == 1
    assert svc.shell_service.register_shell.call_args.kwargs["host"] == "mac"
    assert svc.shell_service.insert_events_batch.call_args.kwargs["host"] == "mac"
    svc.shell_service.mark_stopped.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_events_stopped_marks_shell():
    db = make_mock_db()
    svc = NodeService(db)
    svc.shell_service = MagicMock()
    svc.shell_service.register_shell = AsyncMock()
    svc.shell_service.insert_events_batch = AsyncMock(return_value=0)
    svc.shell_service.mark_stopped = AsyncMock()
    await svc.ingest_events("mac", EventBatchIn(shell_name="claude-x", stopped=True))
    svc.shell_service.mark_stopped.assert_awaited_once_with("claude-x")


# ------------------------------------------------- ShellService host dispatch
def _remote_shell(host="mac", status="active", name="claude-x"):
    return MagicMock(host=host, status=status, name=name)


def _svc(monkeypatch):
    monkeypatch.setattr(settings, "local_node_id", "corsair")
    db = make_mock_db()
    svc = ShellService(db, tmux=MagicMock())
    svc.tmux.send_keys = AsyncMock()
    svc.tmux.capture_pane = AsyncMock(return_value="LOCAL")
    svc.tmux.kill_session = AsyncMock()
    svc.tmux.has_session = AsyncMock(return_value=True)
    return svc, db


@pytest.mark.asyncio
async def test_send_input_remote_dispatches(monkeypatch):
    svc, db = _svc(monkeypatch)
    svc.get_shell = AsyncMock(return_value=_remote_shell())
    db.nodes.find_one = AsyncMock(return_value={"_id": "mac", "last_heartbeat_at": _fresh()})
    with patch("aria.nodes.commands.enqueue_command", new=AsyncMock(return_value="c1")), \
         patch("aria.nodes.commands.await_result",
               new=AsyncMock(return_value={"status": "done", "result": {"line": 1, "screen": "REMOTE"}})):
        line, screen = await svc.send_input("claude-x", "go", wait_ms=500)
    assert (line, screen) == (1, "REMOTE")
    svc.tmux.send_keys.assert_not_awaited()  # went to the node, not local tmux


@pytest.mark.asyncio
async def test_send_input_remote_offline_returns_zero(monkeypatch):
    svc, db = _svc(monkeypatch)
    svc.get_shell = AsyncMock(return_value=_remote_shell())
    db.nodes.find_one = AsyncMock(return_value=None)  # node not registered → offline
    line, screen = await svc.send_input("claude-x", "go")
    assert (line, screen) == (0, None)
    svc.tmux.send_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_screen_remote_uses_snapshot(monkeypatch):
    svc, db = _svc(monkeypatch)
    svc.get_shell = AsyncMock(return_value=_remote_shell())
    svc.get_last_snapshot = AsyncMock(return_value=MagicMock(content="SNAP"))
    assert await svc.current_screen("claude-x") == "SNAP"
    svc.tmux.capture_pane.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_alive_remote(monkeypatch):
    svc, db = _svc(monkeypatch)
    db.nodes.find_one = AsyncMock(return_value={"_id": "mac", "last_heartbeat_at": _fresh()})
    svc.get_shell = AsyncMock(return_value=_remote_shell(status="active"))
    assert await svc.session_alive("claude-x") is True
    svc.get_shell = AsyncMock(return_value=_remote_shell(status="stopped"))
    assert await svc.session_alive("claude-x") is False


@pytest.mark.asyncio
async def test_kill_shell_remote_dispatches(monkeypatch):
    svc, db = _svc(monkeypatch)
    svc.get_shell = AsyncMock(return_value=_remote_shell())
    svc.mark_stopped = AsyncMock()
    svc._remote_command = AsyncMock(return_value={"ok": True})
    await svc.kill_shell("claude-x")
    svc._remote_command.assert_awaited_once()
    svc.tmux.kill_session.assert_not_awaited()
    svc.mark_stopped.assert_awaited_once_with("claude-x")


@pytest.mark.asyncio
async def test_local_shell_uses_tmux(monkeypatch):
    """A local-host shell keeps the direct tmux path (no dispatch)."""
    svc, db = _svc(monkeypatch)
    svc.get_shell = AsyncMock(return_value=_remote_shell(host="corsair"))  # == local
    svc.insert_events_batch = AsyncMock(return_value=1)  # skip the DB write path
    await svc.send_input("claude-x", "go")
    svc.tmux.send_keys.assert_awaited_once()


# ------------------------------------------------- remote coding session routing
@pytest.mark.asyncio
async def test_start_session_routes_to_remote(monkeypatch):
    monkeypatch.setattr(settings, "local_node_id", "corsair")
    from tests.test_coding_session import _make_manager
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(return_value={"_id": "x", "status": "running"})
    mgr = _make_manager(db=db)
    mgr._start_remote_shell_session = AsyncMock(
        return_value={"_id": "x", "status": "running", "host": "mac"}
    )
    result = await mgr.start_session(
        workspace="/w", backend="claude-code", prompt="do", host="mac"
    )
    mgr._start_remote_shell_session.assert_awaited_once()
    assert result["host"] == "mac"


@pytest.mark.asyncio
async def test_remote_session_node_unreachable_marks_failed(monkeypatch):
    monkeypatch.setattr(settings, "local_node_id", "corsair")
    from tests.test_coding_session import _make_manager
    db = make_mock_db()
    mgr = _make_manager(db=db)
    mgr.shell_service = MagicMock()
    mgr.shell_service.register_shell = AsyncMock()
    command = MagicMock(argv=["claude", "-p", "hi"], env=None, cwd="/w")
    with patch("aria.nodes.commands.enqueue_command", new=AsyncMock(return_value="c1")), \
         patch("aria.nodes.commands.await_result", new=AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError):
            await mgr._start_remote_shell_session("sid", "mac", command, "/w")
    update = db.coding_sessions.update_one.call_args[0][1]["$set"]
    assert update["status"] == "failed"


@pytest.mark.asyncio
async def test_remote_launch_uses_basename_and_path_prepend(monkeypatch):
    """Regression: the backend builds argv[0] with THIS host's absolute binary
    path; on a remote node ($HOME differs) it must become a bare name resolved
    via a PATH that includes ~/.local/bin, or claude isn't found and the session
    exits immediately."""
    monkeypatch.setattr(settings, "local_node_id", "corsair")
    from tests.test_coding_session import _make_manager
    db = make_mock_db()
    mgr = _make_manager(db=db)
    mgr.shell_service = MagicMock()
    mgr.shell_service.register_shell = AsyncMock()
    command = MagicMock(
        argv=["/home/ben/.local/bin/claude", "--dangerously-skip-permissions", "-p", "hi"],
        env=None, cwd="/w",
    )
    captured = {}

    async def fake_enqueue(db, node, kind, args, **kw):
        captured.update(args)
        return "c1"

    with patch("aria.nodes.commands.enqueue_command", new=fake_enqueue), \
         patch("aria.nodes.commands.await_result",
               new=AsyncMock(return_value={"status": "done", "result": {"shell_name": "x"}})):
        await mgr._start_remote_shell_session("sid", "mac", command, "/w")

    launch = captured["launch"]
    assert "/home/ben/.local/bin/claude" not in launch      # absolute path stripped
    assert "claude --dangerously-skip-permissions" in launch  # bare binary name
    assert 'export PATH="$HOME/.local/bin:$PATH"' in launch    # PATH prepend
    assert " -p " not in launch                               # -p stripped for interactive


# ------------------------------------------------------------- node agent side
def _agent():
    from aria.node.agent import NodeAgent
    a = NodeAgent("http://x:8200", "k", "mac")
    a.tmux = MagicMock()
    a.tmux.send_keys = AsyncMock()
    a.tmux.kill_session = AsyncMock()
    a.tmux.new_session = AsyncMock()
    a.tmux.capture_pane = AsyncMock(return_value="PANE")
    return a


@pytest.mark.asyncio
async def test_agent_exec_send_input():
    a = _agent()
    out = await a._exec("send_input", {"name": "claude-x", "text": "go", "wait_ms": 0})
    assert out["line"] == 1 and out["screen"] is None
    a.tmux.send_keys.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_exec_stop_and_start():
    a = _agent()
    assert (await a._exec("stop", {"name": "claude-x"}))["ok"] is True
    a.tmux.kill_session.assert_awaited_once_with("claude-x")
    out = await a._exec("start_session", {"shell_name": "claude-coding-1", "launch": "bash -lc x", "workdir": "/w"})
    assert out["shell_name"] == "claude-coding-1"
    a.tmux.new_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_exec_run_command_exit_code_and_tail():
    a = _agent()
    out = await a._exec("run_command", {"command": "echo hello && exit 3", "cwd": "/tmp"})
    assert out["exit_code"] == 3
    assert "hello" in out["output_tail"]

    out = await a._exec("run_command", {"command": "true"})
    assert out["exit_code"] == 0


@pytest.mark.asyncio
async def test_agent_exec_run_command_timeout():
    a = _agent()
    out = await a._exec(
        "run_command", {"command": "sleep 5", "timeout_seconds": 0.2}
    )
    assert out["exit_code"] == -1
    assert out.get("timed_out") is True


@pytest.mark.asyncio
async def test_agent_exec_unknown_kind_raises():
    a = _agent()
    with pytest.raises(ValueError):
        await a._exec("bogus", {})


def test_shellevent_accepts_node_capture_source():
    """Regression: node-pushed events use source='node-capture'; it must be a
    valid ShellEvent source or reading them back (fleet_overview) 500s."""
    from datetime import datetime, timezone
    from aria.shells.models import ShellEvent
    ev = ShellEvent(
        shell_name="nodeloop-1", ts=datetime.now(timezone.utc), line_number=1,
        kind="output", text_raw="hi", text_clean="hi", source="node-capture",
    )
    assert ev.source == "node-capture"


def test_agent_delta_lines_incremental():
    a = _agent()
    # First capture: only the current last line is emitted (no flood).
    assert a._delta_lines("s", "line1\nline2") == ["line2"]
    # Next capture appends line3 → only the new line is emitted.
    assert a._delta_lines("s", "line1\nline2\nline3") == ["line3"]
