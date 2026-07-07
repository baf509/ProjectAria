"""
ARIA - Tests for the per-session Ralph loop

Covers loop-config normalization + toggling on the session manager, and the
watchdog's nudge decision logic (idle→nudge, done/cap/deadline→end, safety
leash→end without nudging).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.agents.session import CodingSessionManager
from aria.agents.watchdog import CodingWatchdog
from aria.config import settings
from tests.conftest import make_mock_db
from tests.test_coding_session import _make_manager


# ---------------------------------------------------------------------------
# Loop config: normalization + toggle
# ---------------------------------------------------------------------------

def test_normalize_fills_defaults_from_settings():
    cfg = CodingSessionManager._normalize_loop_config({})
    assert cfg["idle_seconds"] == settings.coding_loop_idle_seconds
    assert cfg["max_nudges"] == settings.coding_loop_max_nudges
    assert cfg["deadline_minutes"] == settings.coding_loop_deadline_minutes
    assert cfg["done_regex"] == settings.coding_loop_done_regex
    assert cfg["nudge_prompt"] == settings.coding_loop_nudge_prompt
    assert cfg["notify_every"] == 0


def test_normalize_preserves_overrides():
    cfg = CodingSessionManager._normalize_loop_config(
        {"idle_seconds": 10, "max_nudges": 3, "done_regex": "ALLDONE"}
    )
    assert cfg["idle_seconds"] == 10
    assert cfg["max_nudges"] == 3
    assert cfg["done_regex"] == "ALLDONE"
    # unspecified fields still fall back to defaults
    assert cfg["deadline_minutes"] == settings.coding_loop_deadline_minutes


@pytest.mark.asyncio
async def test_set_loop_config_enable_resets_counters():
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(
        return_value={"_id": "s1", "status": "running", "loop_config": None}
    )
    mgr = _make_manager(db=db)

    await mgr.set_loop_config("s1", {"idle_seconds": 5})

    update = db.coding_sessions.update_one.call_args[0][1]["$set"]
    assert update["loop_config"]["idle_seconds"] == 5
    assert update["loop_nudges"] == 0
    assert update["last_nudge_at"] is None
    assert isinstance(update["loop_started_at"], datetime)


@pytest.mark.asyncio
async def test_set_loop_config_disable_clears():
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(
        return_value={"_id": "s1", "status": "running", "loop_config": {"idle_seconds": 5}}
    )
    mgr = _make_manager(db=db)

    await mgr.set_loop_config("s1", None)

    update = db.coding_sessions.update_one.call_args[0][1]["$set"]
    assert update["loop_config"] is None


@pytest.mark.asyncio
async def test_set_loop_config_unknown_session():
    db = make_mock_db()
    db.coding_sessions.find_one = AsyncMock(return_value=None)
    mgr = _make_manager(db=db)
    assert await mgr.set_loop_config("missing", {"idle_seconds": 5}) is None


# ---------------------------------------------------------------------------
# Watchdog nudge logic
# ---------------------------------------------------------------------------

def _make_watchdog(db=None):
    db = db or make_mock_db()
    session_manager = MagicMock()
    session_manager.send_input = AsyncMock(return_value=True)
    session_manager.stop_session = AsyncMock(return_value=True)
    notifier = MagicMock()
    notifier.notify = AsyncMock()
    wd = CodingWatchdog(db, session_manager, notifier, review_service=None)
    return wd, db, session_manager


def _idle_state(seconds_idle=120, output="working...\n> "):
    return {
        "last_changed_at": datetime.now(timezone.utc) - timedelta(seconds=seconds_idle),
        "last_output": output,
    }


def _loop_session(**overrides):
    loop = {
        "nudge_prompt": "keep going",
        "nudge_prompt_file": None,
        "idle_seconds": 45,
        "done_regex": "RALPH_DONE",
        "max_nudges": 40,
        "deadline_minutes": 180,
        "notify_every": 0,
    }
    loop.update(overrides.pop("loop", {}))
    session = {
        "_id": "s1",
        "status": "running",
        "loop_config": loop,
        "loop_nudges": 0,
        "last_nudge_at": None,
        "loop_started_at": datetime.now(timezone.utc) - timedelta(minutes=1),
    }
    session.update(overrides)
    return session


def _patch_gates(estop_active=False, killswitch_raises=False):
    """Patch the safety gates that _maybe_nudge imports from aria.api.deps."""
    ks = MagicMock()
    if killswitch_raises:
        ks.check_or_raise.side_effect = RuntimeError("killswitch engaged")
    estop = MagicMock()
    estop.is_active = AsyncMock(return_value=estop_active)
    return patch.multiple(
        "aria.api.deps",
        get_killswitch=MagicMock(return_value=ks),
        resolve_estop_manager=AsyncMock(return_value=estop),
    )


@pytest.mark.asyncio
async def test_nudge_fires_when_idle():
    wd, db, sm = _make_watchdog()
    session = _loop_session()
    state = _idle_state()
    with _patch_gates():
        await wd._maybe_nudge(session, state)
    sm.send_input.assert_awaited_once_with("s1", "keep going")
    update = db.coding_sessions.update_one.call_args[0][1]["$set"]
    assert update["loop_nudges"] == 1
    sm.stop_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_nudge_before_idle_threshold():
    wd, db, sm = _make_watchdog()
    session = _loop_session()
    state = _idle_state(seconds_idle=5)  # < idle_seconds (45)
    with _patch_gates():
        await wd._maybe_nudge(session, state)
    sm.send_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_done_signal_ends_and_stops():
    wd, db, sm = _make_watchdog()
    session = _loop_session()
    state = _idle_state(output="finished the work\nRALPH_DONE\n")
    with _patch_gates():
        await wd._maybe_nudge(session, state)
    sm.send_input.assert_not_awaited()
    sm.stop_session.assert_awaited_once_with("s1")
    # loop_config cleared so nudging stops
    cleared = [c for c in db.coding_sessions.update_one.call_args_list
               if c[0][1]["$set"].get("loop_config") is None]
    assert cleared


@pytest.mark.asyncio
async def test_max_nudges_ends_and_stops():
    wd, db, sm = _make_watchdog()
    session = _loop_session(loop={"max_nudges": 3}, loop_nudges=3)
    state = _idle_state()
    with _patch_gates():
        await wd._maybe_nudge(session, state)
    sm.send_input.assert_not_awaited()
    sm.stop_session.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_deadline_ends_and_stops():
    wd, db, sm = _make_watchdog()
    session = _loop_session(loop={"deadline_minutes": 10})
    session["loop_started_at"] = datetime.now(timezone.utc) - timedelta(minutes=30)
    state = _idle_state()
    with _patch_gates():
        await wd._maybe_nudge(session, state)
    sm.send_input.assert_not_awaited()
    sm.stop_session.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_estop_ends_loop_without_stopping_or_nudging():
    wd, db, sm = _make_watchdog()
    session = _loop_session()
    state = _idle_state()
    with _patch_gates(estop_active=True):
        await wd._maybe_nudge(session, state)
    sm.send_input.assert_not_awaited()
    # e-stop leaves the session alone (it froze everything already) but stops nudging
    sm.stop_session.assert_not_awaited()
    cleared = [c for c in db.coding_sessions.update_one.call_args_list
               if c[0][1]["$set"].get("loop_config") is None]
    assert cleared


@pytest.mark.asyncio
async def test_killswitch_ends_loop_without_nudging():
    wd, db, sm = _make_watchdog()
    session = _loop_session()
    state = _idle_state()
    with _patch_gates(killswitch_raises=True):
        await wd._maybe_nudge(session, state)
    sm.send_input.assert_not_awaited()
    sm.stop_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_nudge_debounced_by_last_nudge_at():
    wd, db, sm = _make_watchdog()
    # Idle long enough, but we nudged 5s ago (< idle_seconds) → hold off.
    session = _loop_session(last_nudge_at=datetime.now(timezone.utc) - timedelta(seconds=5))
    state = _idle_state()
    with _patch_gates():
        await wd._maybe_nudge(session, state)
    sm.send_input.assert_not_awaited()
