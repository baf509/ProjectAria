"""Tests for the inter-agent mailbox system."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.agents.mail import AgentMailbox, AgentMessage, MessageType
from tests.conftest import make_mock_db


@pytest.fixture
def mock_db():
    db = make_mock_db()
    coll = MagicMock()
    coll.find_one = AsyncMock(return_value=None)
    coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="mock-id"))
    coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    coll.update_many = AsyncMock(return_value=MagicMock(modified_count=0))
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.limit = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=[])
    coll.find = MagicMock(return_value=cursor)
    db.agent_mail = coll
    return db


@pytest.fixture
def mailbox(mock_db):
    return AgentMailbox(mock_db)


class TestMessageType:
    def test_all_types_exist(self):
        assert MessageType.TASK_DONE
        assert MessageType.HANDOFF
        assert MessageType.RESULT
        assert MessageType.ERROR
        assert MessageType.CHECKPOINT


class TestAgentMessage:
    def test_to_dict_roundtrip(self):
        now = datetime.now(timezone.utc)
        msg = AgentMessage(
            sender="coding:claude",
            recipient="orchestrator",
            msg_type=MessageType.TASK_DONE,
            subject="Session complete",
            body="All tests pass",
            metadata={"exit_code": 0},
            created_at=now,
        )
        d = msg.to_dict()
        assert d["sender"] == "coding:claude"
        assert d["type"] == "task_done"
        assert d["read"] is False

        restored = AgentMessage.from_dict(d)
        assert restored.sender == "coding:claude"
        assert restored.msg_type == MessageType.TASK_DONE
        assert restored.body == "All tests pass"


class TestAgentMailbox:
    @pytest.mark.asyncio
    async def test_send(self, mailbox, mock_db):
        msg = AgentMessage(
            sender="agent-a",
            recipient="agent-b",
            msg_type=MessageType.RESULT,
            subject="Test",
            body="Result data",
        )
        await mailbox.send(msg)
        mock_db.agent_mail.insert_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_task_done(self, mailbox, mock_db):
        await mailbox.send_task_done(
            sender="coding:claude",
            recipient="orchestrator",
            session_id="s1",
            result_summary="All tests pass",
            exit_status="completed",
        )
        mock_db.agent_mail.insert_one.assert_awaited_once()
        call_args = mock_db.agent_mail.insert_one.await_args[0][0]
        assert call_args["type"] == "task_done"
        assert call_args["sender"] == "coding:claude"

    @pytest.mark.asyncio
    async def test_send_error(self, mailbox, mock_db):
        await mailbox.send_error(
            sender="watchdog",
            recipient="orchestrator",
            error="Agent stuck in retry loop",
            session_id="s1",
        )
        mock_db.agent_mail.insert_one.assert_awaited_once()
        call_args = mock_db.agent_mail.insert_one.await_args[0][0]
        assert call_args["type"] == "error"

    @pytest.mark.asyncio
    async def test_get_unread_empty(self, mailbox):
        messages = await mailbox.get_unread("agent-b")
        assert messages == []

    @pytest.mark.asyncio
    async def test_get_unread_with_messages(self, mailbox, mock_db):
        now = datetime.now(timezone.utc)
        cursor = MagicMock()
        cursor.sort = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=[
            {
                "_id": "msg1",
                "sender": "agent-a",
                "recipient": "agent-b",
                "type": "result",
                "subject": "Done",
                "body": "Result",
                "metadata": {},
                "created_at": now,
                "read": False,
            }
        ])
        mock_db.agent_mail.find = MagicMock(return_value=cursor)

        messages = await mailbox.get_unread("agent-b")
        assert len(messages) == 1
        assert messages[0].sender == "agent-a"

    @pytest.mark.asyncio
    async def test_mark_read(self, mailbox, mock_db):
        await mailbox.mark_read("msg1")
        mock_db.agent_mail.update_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mark_all_read(self, mailbox, mock_db):
        await mailbox.mark_all_read("agent-b")
        mock_db.agent_mail.update_many.assert_awaited_once()
