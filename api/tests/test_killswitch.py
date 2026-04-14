"""
Tests for aria.core.killswitch.Killswitch.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from aria.core.killswitch import Killswitch


class TestKillswitch:

    def test_initially_inactive(self):
        ks = Killswitch()
        assert ks.is_active is False

    @pytest.mark.asyncio
    async def test_activate(self):
        ks = Killswitch()
        result = await ks.activate(reason="test emergency")

        assert ks.is_active is True
        assert result["active"] is True
        assert result["reason"] == "test emergency"
        assert result["activated_at"] is not None

    @pytest.mark.asyncio
    async def test_deactivate(self):
        ks = Killswitch()
        await ks.activate(reason="test")
        assert ks.is_active is True

        result = await ks.deactivate()
        assert ks.is_active is False
        assert result["active"] is False

    def test_check_or_raise_inactive(self):
        ks = Killswitch()
        # Should not raise
        ks.check_or_raise("some operation")

    @pytest.mark.asyncio
    async def test_check_or_raise_active(self):
        ks = Killswitch()
        await ks.activate(reason="emergency")

        with pytest.raises(RuntimeError, match="Killswitch is active"):
            ks.check_or_raise("some operation")

    @pytest.mark.asyncio
    async def test_status(self):
        ks = Killswitch()
        status = ks.status()
        assert status["active"] is False
        assert status["reason"] is None
        assert status["activated_at"] is None

        await ks.activate(reason="test reason")
        status = ks.status()
        assert status["active"] is True
        assert status["reason"] == "test reason"
        assert status["activated_at"] is not None

    @pytest.mark.asyncio
    async def test_activate_cancels_tasks(self):
        ks = Killswitch()
        task_runner = MagicMock()
        task_runner.cancel_all = AsyncMock(return_value=3)

        result = await ks.activate(reason="stop everything", task_runner=task_runner)

        task_runner.cancel_all.assert_called_once()
        assert result["cancelled_tasks"] == 3

    @pytest.mark.asyncio
    async def test_load_state_persisted(self):
        from datetime import datetime, timezone

        ks = Killswitch()
        db = MagicMock()
        activated_at = datetime.now(timezone.utc)
        db.killswitch = MagicMock()
        db.killswitch.find_one = AsyncMock(return_value={
            "_id": "global",
            "active": True,
            "reason": "persisted reason",
            "activated_at": activated_at,
        })

        await ks.load_state(db)

        assert ks.is_active is True
        status = ks.status()
        assert status["reason"] == "persisted reason"
        assert status["activated_at"] == activated_at
