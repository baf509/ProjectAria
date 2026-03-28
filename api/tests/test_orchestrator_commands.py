"""Tests for CommandRouter — mode, research, memory, coding command handling."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from aria.core.commands import CommandRouter, CommandResult
from aria.memory.extraction import MemoryExtractor
from aria.memory.long_term import LongTermMemory

from tests.conftest import make_mock_db

# Use a valid ObjectId string for all tests
CONV_ID = str(ObjectId())


@pytest.fixture
def command_router():
    db = make_mock_db()
    memory_extractor = MagicMock(spec=MemoryExtractor)
    long_term_memory = MagicMock(spec=LongTermMemory)
    return CommandRouter(
        db=db,
        memory_extractor=memory_extractor,
        long_term_memory=long_term_memory,
        research_service=None,
        coding_manager=None,
    )


# ---------------------------------------------------------------------------
# Mode commands
# ---------------------------------------------------------------------------

class TestModeCommand:
    @pytest.mark.asyncio
    async def test_slash_mode_found(self, command_router):
        agent_doc = {
            "_id": ObjectId(),
            "slug": "creative",
            "name": "Creative Writer",
            "llm": {"backend": "llamacpp", "model": "default", "temperature": 0.9},
        }
        command_router.db.agents.find_one = AsyncMock(return_value=agent_doc)
        command_router.db.conversations.update_one = AsyncMock()

        result = await command_router._handle_mode_command(CONV_ID, "/mode creative")

        assert result is not None
        assert "creative" in result.assistant_content.lower()

    @pytest.mark.asyncio
    async def test_slash_mode_not_found(self, command_router):
        command_router.db.agents.find_one = AsyncMock(return_value=None)

        result = await command_router._handle_mode_command(CONV_ID, "/mode nonexistent")

        assert result is not None
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_slash_mode_trailing_space_is_not_command(self, command_router):
        # "/mode " gets stripped to "/mode" which doesn't match "/mode <slug>"
        result = await command_router._handle_mode_command(CONV_ID, "/mode ")
        assert result is None

    @pytest.mark.asyncio
    async def test_slash_mode_whitespace_slug(self, command_router):
        # "/mode   " inner whitespace still strips to empty slug
        command_router.db.agents.find_one = AsyncMock(return_value=None)
        result = await command_router._handle_mode_command(CONV_ID, "/mode   x")
        # "x" is a valid slug attempt, returns not-found error
        assert result is not None
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_non_mode_message_returns_none(self, command_router):
        result = await command_router._handle_mode_command(CONV_ID, "Hello there!")
        assert result is None

    @pytest.mark.asyncio
    async def test_switch_to_pattern(self, command_router):
        agent_doc = {
            "_id": ObjectId(),
            "slug": "analyst",
            "name": "Data Analyst",
            "llm": {"backend": "llamacpp", "model": "default", "temperature": 0.5},
        }
        command_router.db.agents.find_one = AsyncMock(return_value=agent_doc)
        command_router.db.conversations.update_one = AsyncMock()

        result = await command_router._handle_mode_command(CONV_ID, "switch to analyst mode")

        assert result is not None
        assert "analyst" in result.assistant_content.lower()

    @pytest.mark.asyncio
    async def test_use_pattern(self, command_router):
        agent_doc = {
            "_id": ObjectId(),
            "slug": "coder",
            "name": "Coder",
            "llm": {"backend": "llamacpp", "model": "default", "temperature": 0.3},
        }
        command_router.db.agents.find_one = AsyncMock(return_value=agent_doc)
        command_router.db.conversations.update_one = AsyncMock()

        result = await command_router._handle_mode_command(CONV_ID, "use coder mode")

        assert result is not None
        assert "coder" in result.assistant_content.lower()


# ---------------------------------------------------------------------------
# Research commands
# ---------------------------------------------------------------------------

class TestResearchCommand:
    @pytest.mark.asyncio
    async def test_slash_research(self, command_router):
        mock_research = MagicMock()
        mock_research.start_research = AsyncMock(return_value={
            "research_id": "r1",
            "task_id": "t1",
        })
        command_router.research_service = mock_research
        command_router.db.conversations.update_one = AsyncMock()

        result = await command_router._handle_research_command(CONV_ID, '/research "best databases"')

        assert result is not None
        assert "research" in result.assistant_content.lower()
        assert "r1" in result.assistant_content

    @pytest.mark.asyncio
    async def test_research_prefix(self, command_router):
        mock_research = MagicMock()
        mock_research.start_research = AsyncMock(return_value={
            "research_id": "r2",
            "task_id": "t2",
        })
        command_router.research_service = mock_research
        command_router.db.conversations.update_one = AsyncMock()

        result = await command_router._handle_research_command(CONV_ID, "research best databases")

        assert result is not None
        mock_research.start_research.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_research_returns_none(self, command_router):
        result = await command_router._handle_research_command(CONV_ID, "What is Python?")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_research_service(self):
        db = make_mock_db()
        memory_extractor = MagicMock(spec=MemoryExtractor)
        long_term_memory = MagicMock(spec=LongTermMemory)
        router = CommandRouter(
            db=db,
            memory_extractor=memory_extractor,
            long_term_memory=long_term_memory,
            research_service=None,
        )
        result = await router._handle_research_command(CONV_ID, "/research test")
        assert result is None


# ---------------------------------------------------------------------------
# Memory commands
# ---------------------------------------------------------------------------

class TestMemoryCommand:
    @pytest.mark.asyncio
    async def test_remember_that(self, command_router):
        command_router.memory_extractor.extract_from_text = AsyncMock(return_value=[
            {"content": "Ben likes Python", "content_type": "preference", "categories": [], "importance": 0.7}
        ])
        command_router.long_term_memory.create_memory = AsyncMock(return_value="m1")

        result = await command_router._handle_memory_command(CONV_ID, "remember that I like Python")

        assert result is not None
        assert "remember" in result.assistant_content.lower()
        command_router.long_term_memory.create_memory.assert_awaited()

    @pytest.mark.asyncio
    async def test_forget_about(self, command_router):
        from aria.memory.long_term import Memory
        mem = Memory(
            id="m1", content="test", content_type="fact",
            categories=[], importance=0.5, created_at=datetime.now(timezone.utc), source={},
        )
        command_router.long_term_memory.search = AsyncMock(return_value=[mem])
        command_router.long_term_memory.delete_memory = AsyncMock(return_value=True)

        result = await command_router._handle_memory_command(CONV_ID, "forget about my test memory")

        assert result is not None
        assert "1" in result.assistant_content
        command_router.long_term_memory.delete_memory.assert_awaited_with("m1")

    @pytest.mark.asyncio
    async def test_what_do_you_know(self, command_router):
        from aria.memory.long_term import Memory
        mem = Memory(
            id="m1", content="Python is popular",
            content_type="fact", categories=[], importance=0.5,
            created_at=datetime.now(timezone.utc), source={},
        )
        command_router.long_term_memory.search = AsyncMock(return_value=[mem])

        result = await command_router._handle_memory_command(CONV_ID, "what do you know about Python")

        assert result is not None
        assert "Python is popular" in result.assistant_content

    @pytest.mark.asyncio
    async def test_show_memories(self, command_router):
        cursor = MagicMock()
        cursor.sort = MagicMock(return_value=cursor)
        cursor.limit = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=[
            {"content": "fact 1"},
            {"content": "fact 2"},
        ])
        command_router.db.memories.find = MagicMock(return_value=cursor)

        result = await command_router._handle_memory_command(CONV_ID, "what do you remember?")

        assert result is not None
        assert "fact 1" in result.assistant_content
        assert "fact 2" in result.assistant_content

    @pytest.mark.asyncio
    async def test_non_memory_returns_none(self, command_router):
        result = await command_router._handle_memory_command(CONV_ID, "Hello!")
        assert result is None


# ---------------------------------------------------------------------------
# Coding commands
# ---------------------------------------------------------------------------

class TestCodingCommand:
    @pytest.mark.asyncio
    async def test_no_coding_manager(self, command_router):
        result = await command_router._handle_coding_command(CONV_ID, "/code fix the bug")
        assert result is None

    @pytest.mark.asyncio
    async def test_code_command(self, command_router):
        mock_manager = MagicMock()
        mock_manager.start_session = AsyncMock(return_value={
            "_id": "sess1",
            "workspace": "/home/ben/Dev/ProjectAria",
            "backend": "codex",
        })
        command_router.coding_manager = mock_manager

        result = await command_router._handle_coding_command(CONV_ID, "/code fix the login bug")

        assert result is not None
        assert "sess1" in result.assistant_content
        mock_manager.start_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_coding_status(self, command_router):
        mock_manager = MagicMock()
        mock_manager.list_sessions = AsyncMock(return_value=[
            {"_id": "sess1", "backend": "codex"}
        ])
        mock_manager.get_output = MagicMock(return_value="Running tests...")
        command_router.coding_manager = mock_manager

        result = await command_router._handle_coding_command(CONV_ID, "how's the coding going?")

        assert result is not None
        assert "sess1" in result.assistant_content
        assert "Running tests..." in result.assistant_content

    @pytest.mark.asyncio
    async def test_coding_stop(self, command_router):
        mock_manager = MagicMock()
        mock_manager.list_sessions = AsyncMock(return_value=[
            {"_id": "sess1"}
        ])
        mock_manager.stop_session = AsyncMock(return_value=True)
        command_router.coding_manager = mock_manager

        result = await command_router._handle_coding_command(CONV_ID, "/coding-stop")

        assert result is not None
        assert "Stopped" in result.assistant_content


# ---------------------------------------------------------------------------
# try_handle integration
# ---------------------------------------------------------------------------

class TestTryHandle:
    @pytest.mark.asyncio
    async def test_try_handle_dispatches_mode(self, command_router):
        agent_doc = {
            "_id": ObjectId(),
            "slug": "creative",
            "name": "Creative Writer",
            "llm": {"backend": "llamacpp", "model": "default", "temperature": 0.9},
        }
        command_router.db.agents.find_one = AsyncMock(return_value=agent_doc)
        command_router.db.conversations.update_one = AsyncMock()

        result = await command_router.try_handle(CONV_ID, "/mode creative")

        assert result is not None
        assert isinstance(result, CommandResult)
        assert "creative" in result.assistant_content.lower()

    @pytest.mark.asyncio
    async def test_try_handle_returns_none_for_normal_message(self, command_router):
        result = await command_router.try_handle(CONV_ID, "Hello, how are you?")
        assert result is None

    @pytest.mark.asyncio
    async def test_try_handle_dispatches_memory(self, command_router):
        command_router.memory_extractor.extract_from_text = AsyncMock(return_value=[
            {"content": "test fact", "content_type": "fact", "categories": [], "importance": 0.7}
        ])
        command_router.long_term_memory.create_memory = AsyncMock(return_value="m1")

        result = await command_router.try_handle(CONV_ID, "remember that the sky is blue")

        assert result is not None
        assert isinstance(result, CommandResult)
        assert "remember" in result.assistant_content.lower()
