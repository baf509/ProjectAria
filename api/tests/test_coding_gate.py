"""
ARIA - Tests for the C1 Verification Gate check-runner, incl. the C8
host-aware branch (remote-node sessions run the check ON the node via
run_command instead of being skipped).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.config import settings
from tests.test_coding_loop import _make_watchdog


def _gate_session(**overrides):
    session = {
        "_id": "s1",
        "status": "running",
        "workspace": "/tmp",
        "loop_config": {"gate_command": None, "gate_timeout": 30},
    }
    session.update(overrides)
    return session


def _enable_gate():
    return patch.object(settings, "coding_gate_enabled", True)


@pytest.mark.asyncio
async def test_gate_disabled_skips():
    wd, db, sm = _make_watchdog()
    with patch.object(settings, "coding_gate_enabled", False):
        assert await wd._run_gate_check(_gate_session()) is None


@pytest.mark.asyncio
async def test_local_gate_passes_and_fails_on_exit_code():
    wd, db, sm = _make_watchdog()
    db.projects.find_one = AsyncMock(return_value=None)
    with _enable_gate():
        session = _gate_session(loop_config={"gate_command": "true", "gate_timeout": 30})
        passed, _ = await wd._run_gate_check(session)
        assert passed is True

        session = _gate_session(loop_config={"gate_command": "false", "gate_timeout": 30})
        passed, _ = await wd._run_gate_check(session)
        assert passed is False


@pytest.mark.asyncio
async def test_local_gate_missing_make_target_skips():
    wd, db, sm = _make_watchdog()
    db.projects.find_one = AsyncMock(return_value=None)
    with _enable_gate():
        session = _gate_session(
            loop_config={
                "gate_command": "echo 'make: *** No rule to make target check.  Stop.'; exit 2",
                "gate_timeout": 30,
            }
        )
        assert await wd._run_gate_check(session) is None


@pytest.mark.asyncio
async def test_remote_gate_runs_on_node_and_passes():
    wd, db, sm = _make_watchdog()
    db.projects.find_one = AsyncMock(return_value=None)
    sm.shell_service.run_node_command = AsyncMock(
        return_value={"exit_code": 0, "output_tail": "42 passed"}
    )
    with _enable_gate(), patch("aria.nodes.is_remote_host", return_value=True):
        session = _gate_session(
            host="bens-macbook-air",
            workspace="/Users/ben/dev/proj",
            loop_config={"gate_command": "make check", "gate_timeout": 60},
        )
        passed, tail = await wd._run_gate_check(session)
    assert passed is True
    assert "42 passed" in tail
    sm.shell_service.run_node_command.assert_awaited_once_with(
        "bens-macbook-air",
        "make check",
        cwd="/Users/ben/dev/proj",
        timeout_seconds=60,
    )


@pytest.mark.asyncio
async def test_remote_gate_nonzero_exit_fails():
    wd, db, sm = _make_watchdog()
    db.projects.find_one = AsyncMock(return_value=None)
    sm.shell_service.run_node_command = AsyncMock(
        return_value={"exit_code": 1, "output_tail": "1 failed"}
    )
    with _enable_gate(), patch("aria.nodes.is_remote_host", return_value=True):
        session = _gate_session(
            host="bens-macbook-air",
            loop_config={"gate_command": "make check", "gate_timeout": 60},
        )
        passed, tail = await wd._run_gate_check(session)
    assert passed is False
    assert "1 failed" in tail


@pytest.mark.asyncio
async def test_remote_gate_offline_node_is_failure_not_skip():
    """Verify-don't-assume: an unreachable node means the work is unverified,
    so the gate FAILS (re-nudge / alert path) rather than promoting to done."""
    wd, db, sm = _make_watchdog()
    db.projects.find_one = AsyncMock(return_value=None)
    sm.shell_service.run_node_command = AsyncMock(return_value=None)
    with _enable_gate(), patch("aria.nodes.is_remote_host", return_value=True):
        session = _gate_session(
            host="bens-macbook-air",
            loop_config={"gate_command": "make check", "gate_timeout": 60},
        )
        passed, tail = await wd._run_gate_check(session)
    assert passed is False
    assert "offline" in tail


@pytest.mark.asyncio
async def test_remote_gate_missing_check_skips():
    wd, db, sm = _make_watchdog()
    db.projects.find_one = AsyncMock(return_value=None)
    sm.shell_service.run_node_command = AsyncMock(
        return_value={
            "exit_code": 2,
            "output_tail": "make: *** No rule to make target 'check'.  Stop.",
        }
    )
    with _enable_gate(), patch("aria.nodes.is_remote_host", return_value=True):
        session = _gate_session(
            host="bens-macbook-air",
            loop_config={"gate_command": "make check", "gate_timeout": 60},
        )
        assert await wd._run_gate_check(session) is None


@pytest.mark.asyncio
async def test_local_host_value_still_runs_locally():
    """A session whose host names THIS machine is not remote — the check runs
    as a local subprocess, not through the node queue."""
    wd, db, sm = _make_watchdog()
    db.projects.find_one = AsyncMock(return_value=None)
    sm.shell_service.run_node_command = AsyncMock()
    with _enable_gate(), patch("aria.nodes.is_remote_host", return_value=False):
        session = _gate_session(
            host="corsair-ai",
            loop_config={"gate_command": "true", "gate_timeout": 30},
        )
        passed, _ = await wd._run_gate_check(session)
    assert passed is True
    sm.shell_service.run_node_command.assert_not_awaited()
