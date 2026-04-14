"""Tests for the emergency stop (estop) system."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.agents.estop import EstopManager, EstopState
from tests.conftest import make_mock_db


@pytest.fixture
def mock_db():
    db = make_mock_db()
    for name in ["estop", "estop_log"]:
        coll = MagicMock()
        coll.find_one = AsyncMock(return_value=None)
        coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="mock-id"))
        coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        cursor = MagicMock()
        cursor.sort = MagicMock(return_value=cursor)
        cursor.limit = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=[])
        coll.find = MagicMock(return_value=cursor)
        setattr(db, name, coll)
    return db


@pytest.fixture
def estop(mock_db):
    return EstopManager(mock_db)


class TestEstopState:
    def test_default_inactive(self):
        state = EstopState()
        assert not state.active
        assert state.reason is None

    def test_to_dict_roundtrip(self):
        now = datetime.now(timezone.utc)
        state = EstopState(
            active=True,
            reason="rate limit",
            triggered_by="watchdog",
            triggered_at=now,
            auto_thaw=True,
        )
        d = state.to_dict()
        assert d["active"] is True
        assert d["reason"] == "rate limit"

        restored = EstopState.from_dict(d)
        assert restored.active is True
        assert restored.triggered_by == "watchdog"


class TestEstopManager:
    @pytest.mark.asyncio
    async def test_initial_state_inactive(self, estop):
        state = await estop.get_state()
        assert not state.active

    @pytest.mark.asyncio
    async def test_activate(self, estop, mock_db):
        state = await estop.activate(reason="test", triggered_by="unit_test")
        assert state.active
        assert state.reason == "test"
        mock_db.estop.update_one.assert_awaited()
        mock_db.estop_log.insert_one.assert_awaited()

    @pytest.mark.asyncio
    async def test_is_active_after_activate(self, estop, mock_db):
        # After activate, the DB mock needs to return the active state
        mock_db.estop.find_one = AsyncMock(return_value={
            "active": True, "reason": "test", "triggered_by": "system",
            "triggered_at": None, "auto_thaw": True,
        })
        await estop.activate(reason="test")
        assert await estop.is_active()

    @pytest.mark.asyncio
    async def test_deactivate(self, estop, mock_db):
        mock_db.estop.find_one = AsyncMock(return_value={
            "active": False, "reason": None, "triggered_by": None,
            "triggered_at": None, "auto_thaw": True,
        })
        await estop.activate(reason="test")
        state = await estop.deactivate(reason="cleared")
        assert not state.active

    @pytest.mark.asyncio
    async def test_cache_invalidation_on_activate(self, estop, mock_db):
        # First call caches inactive state
        await estop.get_state()
        # After activate, DB returns active
        mock_db.estop.find_one = AsyncMock(return_value={
            "active": True, "reason": "test", "triggered_by": "unit_test",
            "triggered_at": None, "auto_thaw": True,
        })
        await estop.activate(reason="test")
        # get_state should return active
        state = await estop.get_state()
        assert state.active

    @pytest.mark.asyncio
    async def test_get_log(self, estop, mock_db):
        mock_cursor = MagicMock()
        mock_cursor.sort = MagicMock(return_value=mock_cursor)
        mock_cursor.limit = MagicMock(return_value=mock_cursor)
        mock_cursor.to_list = AsyncMock(return_value=[
            {"action": "activate", "reason": "test"},
        ])
        mock_db.estop_log.find = MagicMock(return_value=mock_cursor)

        log = await estop.get_log(limit=10)
        assert len(log) == 1
        assert log[0]["action"] == "activate"
