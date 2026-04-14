"""
ARIA - Context Builder Tests

Tests for aria.core.context.ContextBuilder covering message assembly,
memory injection, soul content, conversation summaries, and truncation.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.core.context import ContextBuilder
from aria.llm.base import Message
from aria.memory.long_term import Memory
from aria.memory.short_term import ConversationSummary
from tests.conftest import make_mock_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent_config(**overrides) -> dict:
    """Build a minimal agent config dict."""
    config = {
        "system_prompt": "You are ARIA.",
        "llm": {"model": "default", "max_context_tokens": 8192, "max_tokens": 1024},
        "capabilities": {"memory_enabled": True, "tools_enabled": False},
        "memory_config": {"short_term_messages": 20, "long_term_results": 5},
    }
    config.update(overrides)
    return config


def _make_memory(content: str, content_type: str = "fact") -> Memory:
    """Create a Memory instance for testing."""
    return Memory(
        id="mem-1",
        content=content,
        content_type=content_type,
        categories=["test"],
        importance=0.8,
        created_at=datetime(2026, 1, 1),
        source={"type": "test"},
    )


def _make_context_builder(db=None):
    """Create a ContextBuilder with mocked sub-components."""
    db = db or make_mock_db()
    cb = ContextBuilder(db)
    cb.short_term = MagicMock()
    cb.short_term.get_current_conversation_context = AsyncMock(return_value=[])
    cb.short_term.get_recent_conversations_context = AsyncMock(return_value=[])
    cb.long_term = MagicMock()
    cb.long_term.search = AsyncMock(return_value=[])
    cb.long_term.batch_increment_access = AsyncMock()
    return cb


# Shared patches applied to every build_messages test
_common_patches = [
    patch("aria.core.context.truncate_to_budget", side_effect=lambda msgs, *a, **kw: msgs),
    patch("aria.core.context.count_tokens", return_value=10),
    patch("aria.core.context.soul_manager"),
    patch("aria.core.context.settings"),
]


def _apply_common_patches(soul_read=None, awareness_enabled=False, deep_think_enabled=False):
    """Start common patches and return (mock_truncate, mock_count, mock_soul, mock_settings)."""
    started = [p.start() for p in _common_patches]
    mock_truncate, mock_count, mock_soul, mock_settings = started
    mock_soul.read.return_value = soul_read
    mock_settings.awareness_enabled = awareness_enabled
    mock_settings.awareness_inject_context = awareness_enabled
    mock_settings.deep_think_enabled = deep_think_enabled
    return mock_truncate, mock_count, mock_soul, mock_settings


def _stop_common_patches():
    for p in _common_patches:
        p.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_messages_basic():
    """Basic call returns system prompt + user message."""
    _apply_common_patches()
    try:
        cb = _make_context_builder()
        msgs = await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="Hello",
            agent_config=_make_agent_config(),
        )
        assert len(msgs) >= 2
        assert msgs[0].role == "system"
        assert "You are ARIA." in msgs[0].content
        assert msgs[-1].role == "user"
        assert msgs[-1].content == "Hello"
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_build_messages_with_soul():
    """SOUL.md content is injected into the system prompt."""
    _apply_common_patches(soul_read="I am ARIA, a personal AI agent.")
    try:
        cb = _make_context_builder()
        msgs = await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="Hi",
            agent_config=_make_agent_config(),
        )
        system_content = msgs[0].content
        assert "## Agent Identity (SOUL)" in system_content
        assert "I am ARIA, a personal AI agent." in system_content
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_build_messages_with_conversation_summary():
    """Conversation summary is injected into the system prompt."""
    _apply_common_patches()
    try:
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value={"summary": "We discussed testing."})
        cb = _make_context_builder(db)
        msgs = await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="Continue",
            agent_config=_make_agent_config(),
        )
        system_content = msgs[0].content
        assert "## Conversation Summary" in system_content
        assert "We discussed testing." in system_content
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_build_messages_with_memories():
    """Long-term memories are injected into the system prompt."""
    _apply_common_patches()
    try:
        cb = _make_context_builder()
        memories = [
            _make_memory("User prefers dark mode", "preference"),
            _make_memory("User works on ProjectAria", "fact"),
        ]
        cb.long_term.search = AsyncMock(return_value=memories)

        msgs = await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="What do you know about me?",
            agent_config=_make_agent_config(),
        )
        system_content = msgs[0].content
        assert "## Relevant Long-Term Memories" in system_content
        assert "User prefers dark mode" in system_content
        assert "User works on ProjectAria" in system_content
        assert "[preference]" in system_content
        assert "[fact]" in system_content
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_build_messages_no_memories_when_disabled():
    """include_memories=False skips long-term memory search entirely."""
    _apply_common_patches()
    try:
        cb = _make_context_builder()
        msgs = await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="Hello",
            agent_config=_make_agent_config(),
            include_memories=False,
        )
        cb.long_term.search.assert_not_called()
        assert "## Relevant Long-Term Memories" not in msgs[0].content
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_build_messages_private_conversation_filters():
    """private=True passes no filter on private memories (allows all)."""
    _apply_common_patches()
    try:
        cb = _make_context_builder()
        cb.long_term.search = AsyncMock(return_value=[])

        await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="Secret stuff",
            agent_config=_make_agent_config(),
            private=True,
        )
        # With private=True, filters should be None (no exclusion of private memories)
        call_kwargs = cb.long_term.search.call_args
        assert call_kwargs.kwargs.get("filters") is None or call_kwargs[1].get("filters") is None
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_build_messages_non_private_excludes_private_memories():
    """private=False (default) excludes private memories via filter."""
    _apply_common_patches()
    try:
        cb = _make_context_builder()
        cb.long_term.search = AsyncMock(return_value=[])

        await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="Public question",
            agent_config=_make_agent_config(),
            private=False,
        )
        call_kwargs = cb.long_term.search.call_args
        filters = call_kwargs.kwargs.get("filters") or call_kwargs[1].get("filters")
        assert filters == {"private": {"$ne": True}}
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_build_messages_parallel_fetch():
    """Memories and short-term context are fetched concurrently via asyncio.gather."""
    _apply_common_patches()
    try:
        call_order = []

        async def mock_search(**kwargs):
            call_order.append("search_start")
            await asyncio.sleep(0.01)
            call_order.append("search_end")
            return []

        async def mock_context(**kwargs):
            call_order.append("context_start")
            await asyncio.sleep(0.01)
            call_order.append("context_end")
            return []

        cb = _make_context_builder()
        cb.long_term.search = mock_search
        cb.short_term.get_current_conversation_context = mock_context

        await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="Test parallel",
            agent_config=_make_agent_config(),
        )

        # Both should have started before either finished (concurrent execution)
        assert "search_start" in call_order
        assert "context_start" in call_order
        search_start_idx = call_order.index("search_start")
        context_start_idx = call_order.index("context_start")
        search_end_idx = call_order.index("search_end")
        context_end_idx = call_order.index("context_end")
        # Both start before both end (interleaved)
        assert max(search_start_idx, context_start_idx) < min(search_end_idx, context_end_idx)
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_build_messages_user_message_appended_last():
    """The current user message is always the last message in the list."""
    _apply_common_patches()
    try:
        cb = _make_context_builder()
        cb.short_term.get_current_conversation_context = AsyncMock(return_value=[
            {"role": "user", "content": "old message"},
            {"role": "assistant", "content": "old reply"},
        ])

        msgs = await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="Latest question",
            agent_config=_make_agent_config(),
        )
        assert msgs[-1].role == "user"
        assert msgs[-1].content == "Latest question"
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_build_messages_conversation_messages_included():
    """Short-term conversation messages appear between system and user message."""
    _apply_common_patches()
    try:
        cb = _make_context_builder()
        cb.short_term.get_current_conversation_context = AsyncMock(return_value=[
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ])

        msgs = await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="New question",
            agent_config=_make_agent_config(),
        )
        assert msgs[0].role == "system"
        assert msgs[1].role == "user"
        assert msgs[1].content == "previous question"
        assert msgs[2].role == "assistant"
        assert msgs[2].content == "previous answer"
        assert msgs[3].role == "user"
        assert msgs[3].content == "New question"
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_build_messages_with_deep_think():
    """deep_think_enabled injects delegation instructions into system prompt."""
    _apply_common_patches(deep_think_enabled=True)
    try:
        cb = _make_context_builder()
        msgs = await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="Explain something complex",
            agent_config=_make_agent_config(),
        )
        system_content = msgs[0].content
        assert "## Reasoning Delegation (IMPORTANT)" in system_content
        assert "deep_think" in system_content
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_build_messages_memory_disabled_in_capabilities():
    """memory_enabled=False in capabilities skips memory search."""
    _apply_common_patches()
    try:
        cb = _make_context_builder()
        config = _make_agent_config()
        config["capabilities"]["memory_enabled"] = False

        await cb.build_messages(
            conversation_id="aabbccddee112233aabbccdd",
            user_message="Hello",
            agent_config=config,
        )
        cb.long_term.search.assert_not_called()
    finally:
        _stop_common_patches()


@pytest.mark.asyncio
async def test_get_recent_context_summary_empty():
    """No recent conversations returns default message."""
    cb = _make_context_builder()
    cb.short_term.get_recent_conversations_context = AsyncMock(return_value=[])

    result = await cb.get_recent_context_summary(hours=24, limit=5)
    assert result == "No recent conversations."


@pytest.mark.asyncio
async def test_get_recent_context_summary_with_data():
    """Formats conversation summaries correctly."""
    cb = _make_context_builder()
    summaries = [
        ConversationSummary(
            id="conv-1",
            title="Testing Discussion",
            summary="Talked about unit tests",
            updated_at=datetime(2026, 4, 7, 10, 30),
        ),
        ConversationSummary(
            id="conv-2",
            title="Architecture Chat",
            summary=None,
            updated_at=datetime(2026, 4, 7, 14, 0),
        ),
    ]
    cb.short_term.get_recent_conversations_context = AsyncMock(return_value=summaries)

    result = await cb.get_recent_context_summary(hours=24, limit=5)
    assert "Recent conversations:" in result
    assert "Testing Discussion (2026-04-07 10:30): Talked about unit tests" in result
    assert "Architecture Chat (2026-04-07 14:00)" in result
    # The one without summary should not have a trailing colon+text
    assert "Architecture Chat (2026-04-07 14:00):" not in result
