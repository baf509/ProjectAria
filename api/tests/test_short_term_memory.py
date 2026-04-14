"""
ARIA - Tests for Short-Term Memory

Tests for ShortTermMemory and ConversationSummary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from aria.memory.short_term import ConversationSummary, ShortTermMemory
from tests.conftest import make_mock_db


# ---------------------------------------------------------------------------
# ConversationSummary
# ---------------------------------------------------------------------------


def test_conversation_summary_from_doc():
    """ConversationSummary.from_doc creates object from MongoDB document."""
    oid = ObjectId()
    doc = {
        "_id": oid,
        "title": "Planning session",
        "summary": "Discussed roadmap",
        "updated_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
    }
    cs = ConversationSummary.from_doc(doc)

    assert cs.id == str(oid)
    assert cs.title == "Planning session"
    assert cs.summary == "Discussed roadmap"
    assert cs.updated_at == datetime(2026, 4, 1, tzinfo=timezone.utc)


def test_conversation_summary_from_doc_no_summary():
    """from_doc handles missing summary field."""
    doc = {
        "_id": ObjectId(),
        "title": "Quick chat",
        "updated_at": datetime(2026, 4, 2, tzinfo=timezone.utc),
    }
    cs = ConversationSummary.from_doc(doc)
    assert cs.summary is None


# ---------------------------------------------------------------------------
# ShortTermMemory.get_current_conversation_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("aria.memory.short_term.count_tokens", return_value=10)
async def test_get_context_empty(mock_ct):
    """Returns empty list when conversation is not found."""
    db = make_mock_db()
    db.conversations.find_one = AsyncMock(return_value=None)
    stm = ShortTermMemory(db)

    result = await stm.get_current_conversation_context("aabbccddee112233aabbccdd")
    assert result == []


@pytest.mark.asyncio
@patch("aria.memory.short_term.count_tokens", return_value=10)
async def test_get_context_returns_messages(mock_ct):
    """Returns messages from the conversation document."""
    db = make_mock_db()
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    db.conversations.find_one = AsyncMock(return_value={
        "_id": ObjectId(),
        "messages": messages,
    })
    stm = ShortTermMemory(db)

    result = await stm.get_current_conversation_context("aabbccddee112233aabbccdd")

    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert result[1]["role"] == "assistant"


@pytest.mark.asyncio
@patch("aria.memory.short_term.count_tokens", return_value=10)
async def test_get_context_trims_to_tokens(mock_ct):
    """Respects token budget by trimming older messages."""
    db = make_mock_db()
    messages = [
        {"role": "user", "content": f"Message {i}"} for i in range(10)
    ]
    db.conversations.find_one = AsyncMock(return_value={
        "_id": ObjectId(),
        "messages": messages,
    })
    stm = ShortTermMemory(db)

    # Each message = 10 tokens + 4 overhead = 14 tokens
    # Budget = 30 tokens -> fits 2 messages (28 tokens), 3rd would be 42
    result = await stm.get_current_conversation_context(
        "aabbccddee112233aabbccdd", max_tokens=30
    )

    assert len(result) == 2
    # Should keep the most recent messages
    assert result[-1]["content"] == "Message 9"
    assert result[0]["content"] == "Message 8"


# ---------------------------------------------------------------------------
# ShortTermMemory._trim_to_tokens
# ---------------------------------------------------------------------------


@patch("aria.memory.short_term.count_tokens", return_value=10)
def test_trim_to_tokens_keeps_most_recent(mock_ct):
    """Trims oldest messages first, keeping most recent."""
    stm = ShortTermMemory(MagicMock())
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "older reply"},
        {"role": "user", "content": "recent"},
        {"role": "assistant", "content": "latest reply"},
    ]

    # 14 tokens per message, budget = 30 -> 2 messages
    result = stm._trim_to_tokens(messages, max_tokens=30, model="default")

    assert len(result) == 2
    assert result[0]["content"] == "recent"
    assert result[1]["content"] == "latest reply"


@patch("aria.memory.short_term.count_tokens", return_value=5000)
def test_trim_to_tokens_single_message_over_budget(mock_ct):
    """Returns single message even if it exceeds budget (first message checked)."""
    stm = ShortTermMemory(MagicMock())
    messages = [{"role": "user", "content": "x" * 10000}]

    result = stm._trim_to_tokens(messages, max_tokens=100, model="default")

    assert len(result) == 1
    assert result[0]["content"] == "x" * 10000


@patch("aria.memory.short_term.count_tokens", return_value=10)
def test_trim_to_tokens_empty(mock_ct):
    """Empty message list returns empty list."""
    stm = ShortTermMemory(MagicMock())
    result = stm._trim_to_tokens([], max_tokens=1000, model="default")
    assert result == []


# ---------------------------------------------------------------------------
# ShortTermMemory.get_recent_conversations_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_conversations():
    """Returns ConversationSummary objects from recent conversations."""
    db = make_mock_db()
    oid1 = ObjectId()
    oid2 = ObjectId()
    docs = [
        {"_id": oid1, "title": "Chat A", "summary": "About X", "updated_at": datetime(2026, 4, 7, tzinfo=timezone.utc)},
        {"_id": oid2, "title": "Chat B", "summary": None, "updated_at": datetime(2026, 4, 6, tzinfo=timezone.utc)},
    ]
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.limit = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=docs)
    db.conversations.find = MagicMock(return_value=cursor)

    stm = ShortTermMemory(db)
    result = await stm.get_recent_conversations_context(hours=24, limit=5)

    assert len(result) == 2
    assert isinstance(result[0], ConversationSummary)
    assert result[0].title == "Chat A"
    assert result[1].summary is None


# ---------------------------------------------------------------------------
# ShortTermMemory.archive_old_conversations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_old_conversations():
    """archive_old_conversations calls update_many on the DB."""
    db = make_mock_db()
    db.conversations.update_many = AsyncMock(
        return_value=MagicMock(modified_count=5)
    )
    stm = ShortTermMemory(db)

    count = await stm.archive_old_conversations(days=90, batch_size=100)

    assert count == 5
    db.conversations.update_many.assert_awaited_once()
    call_args = db.conversations.update_many.call_args
    query = call_args[0][0]
    update = call_args[0][1]
    assert query["status"] == "active"
    assert "$lt" in query["updated_at"]
    assert update["$set"]["status"] == "archived"
