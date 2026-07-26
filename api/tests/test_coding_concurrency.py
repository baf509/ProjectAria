"""Tests for the coding-session concurrency limiter + wait_for_session join
primitive (Pi-Flow parity, Phases 0 & 1).

The slot primitives are tested directly on a bare manager (via __new__) so the
tests don't drag in the backend registry / shell service side effects.
"""

import asyncio

import pytest

from aria.agents.mail import AgentMessage, MessageType
from aria.agents.session import CodingSessionManager


def _bare_manager(limit: int) -> CodingSessionManager:
    mgr = CodingSessionManager.__new__(CodingSessionManager)
    mgr._slot_limit = limit
    mgr._slot_cv = asyncio.Condition()
    mgr._active = 0
    mgr._slotted = set()
    return mgr


# ---------------------------------------------------------------------------
# Slot primitives
# ---------------------------------------------------------------------------

class TestSlots:
    @pytest.mark.asyncio
    async def test_nowait_reserves_until_capacity(self):
        mgr = _bare_manager(limit=2)
        assert await mgr._try_acquire_slot_nowait("a") is True
        assert await mgr._try_acquire_slot_nowait("b") is True
        assert mgr._active == 2
        # At capacity — third is refused.
        assert await mgr._try_acquire_slot_nowait("c") is False
        assert mgr._active == 2

    @pytest.mark.asyncio
    async def test_nowait_is_idempotent_for_same_id(self):
        mgr = _bare_manager(limit=1)
        assert await mgr._try_acquire_slot_nowait("a") is True
        # Same id again doesn't consume a second slot.
        assert await mgr._try_acquire_slot_nowait("a") is True
        assert mgr._active == 1

    @pytest.mark.asyncio
    async def test_release_frees_capacity(self):
        mgr = _bare_manager(limit=1)
        assert await mgr._try_acquire_slot_nowait("a") is True
        assert await mgr._try_acquire_slot_nowait("b") is False
        await mgr._release_slot("a")
        assert mgr._active == 0
        assert await mgr._try_acquire_slot_nowait("b") is True

    @pytest.mark.asyncio
    async def test_release_is_idempotent_and_never_negative(self):
        mgr = _bare_manager(limit=1)
        await mgr._try_acquire_slot_nowait("a")
        await mgr._release_slot("a")
        await mgr._release_slot("a")          # double release
        await mgr._release_slot("unknown")    # never acquired
        assert mgr._active == 0

    @pytest.mark.asyncio
    async def test_unlimited_when_limit_zero(self):
        mgr = _bare_manager(limit=0)
        for i in range(50):
            assert await mgr._try_acquire_slot_nowait(str(i)) is True
        assert mgr._active == 50

    @pytest.mark.asyncio
    async def test_blocking_acquire_wakes_on_release(self):
        mgr = _bare_manager(limit=1)
        await mgr._acquire_slot("a")

        waiter = asyncio.create_task(mgr._acquire_slot("b"))
        await asyncio.sleep(0.02)
        assert not waiter.done()  # still blocked at capacity

        await mgr._release_slot("a")
        await asyncio.wait_for(waiter, timeout=1.0)
        assert "b" in mgr._slotted
        assert mgr._active == 1


# ---------------------------------------------------------------------------
# wait_for_session join primitive
# ---------------------------------------------------------------------------

class TestWaitForSession:
    @pytest.mark.asyncio
    async def test_polls_until_terminal_and_attaches_summary(self):
        from unittest.mock import AsyncMock, MagicMock

        mgr = CodingSessionManager.__new__(CodingSessionManager)
        # running, running, then completed
        mgr.get_session = AsyncMock(side_effect=[
            {"_id": "s1", "status": "running"},
            {"_id": "s1", "status": "running"},
            {"_id": "s1", "status": "completed"},
        ])
        mgr.mailbox = MagicMock()
        mgr.mailbox.get_session_mail = AsyncMock(return_value=[
            AgentMessage(
                sender="coding:claude_code", recipient="orchestrator",
                msg_type=MessageType.TASK_DONE, subject="done",
                body="final summary here", session_id="s1",
            ),
        ])

        result = await mgr.wait_for_session("s1", poll_interval=0.01)
        assert result["status"] == "completed"
        assert result["result_summary"] == "final summary here"

    @pytest.mark.asyncio
    async def test_timeout_returns_non_terminal(self):
        from unittest.mock import AsyncMock

        mgr = CodingSessionManager.__new__(CodingSessionManager)
        mgr.get_session = AsyncMock(return_value={"_id": "s1", "status": "running"})
        result = await mgr.wait_for_session("s1", timeout=0.05, poll_interval=0.01)
        assert result["timed_out"] is True
        assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_missing_session_returns_none(self):
        from unittest.mock import AsyncMock

        mgr = CodingSessionManager.__new__(CodingSessionManager)
        mgr.get_session = AsyncMock(return_value=None)
        assert await mgr.wait_for_session("nope", poll_interval=0.01) is None
