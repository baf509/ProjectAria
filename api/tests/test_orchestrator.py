"""Tests for Orchestrator — LLM candidate selection, agent resolution, and message processing."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from aria.core.orchestrator import Orchestrator
from aria.core.commands import CommandResult
from aria.llm.base import StreamChunk, ToolCall, Message

from tests.conftest import make_mock_db, FakeLLMAdapter, FakeTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONV_ID = str(ObjectId())
AGENT_ID = str(ObjectId())

DEFAULT_AGENT = {
    "_id": ObjectId(AGENT_ID),
    "slug": "default",
    "name": "Default Agent",
    "llm": {"backend": "llamacpp", "model": "default-model", "temperature": 0.7, "max_tokens": 4096},
    "capabilities": {"tools_enabled": False, "memory_enabled": False},
    "memory_config": {"auto_extract": False},
}

DEFAULT_CONVERSATION = {
    "_id": ObjectId(CONV_ID),
    "agent_id": AGENT_ID,
    "messages": [],
    "stats": {"message_count": 0},
}


def _make_orchestrator(db=None, tool_router=None):
    """Build an Orchestrator with mocked internals."""
    db = db or make_mock_db()
    orch = Orchestrator(db=db, tool_router=tool_router)
    # Replace heavy dependencies with mocks
    orch.context_builder = MagicMock()
    orch.context_builder.build_messages = AsyncMock(return_value=[
        Message(role="system", content="You are ARIA."),
        Message(role="user", content="hello"),
    ])
    orch.command_router = MagicMock()
    orch.command_router.try_handle = AsyncMock(return_value=None)
    orch.command_router.try_handle_contextual = AsyncMock(return_value=None)
    orch.memory_extractor = MagicMock()
    orch.memory_extractor.extract_from_conversation = AsyncMock(return_value=0)
    orch.long_term_memory = MagicMock()
    orch.usage_repo = MagicMock()
    orch.usage_repo.record = AsyncMock()
    return orch


async def _collect_chunks(async_iter) -> list[StreamChunk]:
    """Collect all chunks from an async iterator."""
    chunks = []
    async for chunk in async_iter:
        chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# _get_llm_candidates
# ---------------------------------------------------------------------------

class TestGetLLMCandidates:
    def test_primary_only(self):
        """Agent with no fallback chain yields a single candidate."""
        orch = _make_orchestrator()
        agent = {
            "llm": {"backend": "llamacpp", "model": "m1"},
        }
        candidates = orch._get_llm_candidates(agent)
        assert len(candidates) == 1
        assert candidates[0] == ({"backend": "llamacpp", "model": "m1"}, False)

    def test_with_fallbacks(self):
        """Agent with fallback_chain yields primary + fallbacks."""
        orch = _make_orchestrator()
        agent = {
            "llm": {"backend": "llamacpp", "model": "m1"},
            "fallback_chain": [
                {"backend": "openrouter", "model": "m2", "conditions": {"on_error": True}},
                {"backend": "anthropic", "model": "m3", "conditions": {"on_error": True}},
            ],
        }
        candidates = orch._get_llm_candidates(agent)
        assert len(candidates) == 3
        assert candidates[0][1] is False  # primary is not fallback
        assert candidates[1][1] is True   # first fallback
        assert candidates[2][1] is True   # second fallback
        assert candidates[1][0]["backend"] == "openrouter"
        assert candidates[2][0]["backend"] == "anthropic"

    def test_private_forces_llamacpp(self):
        """Private conversation forces llamacpp backend, no fallbacks."""
        orch = _make_orchestrator()
        agent = {
            "llm": {"backend": "openrouter", "model": "m1"},
            "fallback_chain": [
                {"backend": "anthropic", "model": "m2", "conditions": {"on_error": True}},
            ],
        }
        conversation = {"private": True}
        candidates = orch._get_llm_candidates(agent, conversation)
        assert len(candidates) == 1
        assert candidates[0][0]["backend"] == "llamacpp"
        assert candidates[0][1] is False

    def test_conversation_override(self):
        """Conversation with llm_config_override uses that instead of agent primary."""
        orch = _make_orchestrator()
        agent = {
            "llm": {"backend": "llamacpp", "model": "m1"},
            "fallback_chain": [
                {"backend": "anthropic", "model": "m3", "conditions": {"on_error": True}},
            ],
        }
        conversation = {
            "llm_config_override": {"backend": "openrouter", "model": "override-model"},
        }
        candidates = orch._get_llm_candidates(agent, conversation)
        assert candidates[0][0]["backend"] == "openrouter"
        assert candidates[0][0]["model"] == "override-model"
        # Fallbacks still present
        assert len(candidates) == 2
        assert candidates[1][0]["backend"] == "anthropic"


# ---------------------------------------------------------------------------
# _resolve_active_agent
# ---------------------------------------------------------------------------

class TestResolveActiveAgent:
    @pytest.mark.asyncio
    async def test_resolves_by_agent_id(self):
        db = make_mock_db()
        agent_doc = {**DEFAULT_AGENT}
        db.agents.find_one = AsyncMock(return_value=agent_doc)
        orch = _make_orchestrator(db=db)

        result = await orch._resolve_active_agent({"agent_id": AGENT_ID})
        assert result == agent_doc
        db.agents.find_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_prefers_active_agent_id(self):
        db = make_mock_db()
        active_id = str(ObjectId())
        agent_doc = {**DEFAULT_AGENT, "_id": ObjectId(active_id)}
        db.agents.find_one = AsyncMock(return_value=agent_doc)
        orch = _make_orchestrator(db=db)

        result = await orch._resolve_active_agent({
            "agent_id": AGENT_ID,
            "active_agent_id": active_id,
        })
        assert result == agent_doc
        call_args = db.agents.find_one.call_args[0][0]
        assert call_args["_id"] == ObjectId(active_id)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_agent_id(self):
        orch = _make_orchestrator()
        result = await orch._resolve_active_agent({})
        assert result is None


# ---------------------------------------------------------------------------
# _persist_assistant_message
# ---------------------------------------------------------------------------

class TestPersistAssistantMessage:
    @pytest.mark.asyncio
    async def test_saves_message_to_db(self):
        db = make_mock_db()
        orch = _make_orchestrator(db=db)

        await orch._persist_assistant_message(CONV_ID, "Hello!", "test-model")

        db.conversations.update_one.assert_awaited_once()
        call_args = db.conversations.update_one.call_args
        filter_doc = call_args[0][0]
        update_doc = call_args[0][1]
        assert filter_doc["_id"] == ObjectId(CONV_ID)
        pushed_msg = update_doc["$push"]["messages"]
        assert pushed_msg["role"] == "assistant"
        assert pushed_msg["content"] == "Hello!"
        assert pushed_msg["model"] == "test-model"


# ---------------------------------------------------------------------------
# process_message
# ---------------------------------------------------------------------------

class TestProcessMessage:
    @pytest.mark.asyncio
    async def test_conversation_not_found(self):
        """Yields error chunk when conversation doesn't exist."""
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value=None)
        orch = _make_orchestrator(db=db)

        chunks = await _collect_chunks(
            orch.process_message(CONV_ID, "hello")
        )

        assert len(chunks) == 1
        assert chunks[0].type == "error"
        assert "not found" in chunks[0].error.lower()

    @pytest.mark.asyncio
    async def test_command_handled(self):
        """Command router returns a result, stops processing early."""
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
        orch = _make_orchestrator(db=db)
        orch.command_router.try_handle = AsyncMock(
            return_value=CommandResult(
                assistant_content="Mode switched to creative.",
                persist_message=True,
            )
        )

        chunks = await _collect_chunks(
            orch.process_message(CONV_ID, "/mode creative")
        )

        assert any(c.type == "text" and "Mode switched" in c.content for c in chunks)
        assert any(c.type == "done" for c in chunks)

    @pytest.mark.asyncio
    async def test_command_error(self):
        """Command router returns an error result."""
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
        orch = _make_orchestrator(db=db)
        orch.command_router.try_handle = AsyncMock(
            return_value=CommandResult(
                assistant_content="",
                error="Agent not found",
            )
        )

        chunks = await _collect_chunks(
            orch.process_message(CONV_ID, "/mode nonexistent")
        )

        assert any(c.type == "error" for c in chunks)

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        """Yields error when agent can't be resolved."""
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
        db.agents.find_one = AsyncMock(return_value=None)
        orch = _make_orchestrator(db=db)

        chunks = await _collect_chunks(
            orch.process_message(CONV_ID, "hello")
        )

        assert any(c.type == "error" and "agent" in c.error.lower() for c in chunks)

    @pytest.mark.asyncio
    @patch("aria.core.orchestrator.hook_registry")
    @patch("aria.core.orchestrator.llm_manager")
    async def test_basic_response(self, mock_llm_mgr, mock_hooks):
        """Happy path: saves user msg, gets LLM response, yields text + done."""
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
        db.agents.find_one = AsyncMock(return_value=DEFAULT_AGENT)
        orch = _make_orchestrator(db=db)

        fake = FakeLLMAdapter(response_text="Hi there!")
        mock_llm_mgr.get_adapter.return_value = fake
        mock_llm_mgr.is_backend_healthy = AsyncMock(return_value=True)
        mock_llm_mgr.record_backend_success = AsyncMock()
        mock_llm_mgr.record_backend_failure = AsyncMock()
        mock_llm_mgr.record_fallback = MagicMock()
        mock_hooks.fire = AsyncMock(return_value={})

        chunks = await _collect_chunks(
            orch.process_message(CONV_ID, "hello")
        )

        text_chunks = [c for c in chunks if c.type == "text"]
        done_chunks = [c for c in chunks if c.type == "done"]
        assert len(text_chunks) >= 1
        assert "Hi there!" in "".join(c.content for c in text_chunks)
        assert len(done_chunks) == 1

        # Verify user message was saved
        assert db.conversations.update_one.await_count >= 1

    @pytest.mark.asyncio
    @patch("aria.core.orchestrator.hook_registry")
    @patch("aria.core.orchestrator.llm_manager")
    @patch("aria.core.orchestrator.steering_queue")
    async def test_with_tool_calls(self, mock_steering, mock_llm_mgr, mock_hooks):
        """LLM returns tool calls, tool gets executed, LLM called again."""
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
        agent_with_tools = {
            **DEFAULT_AGENT,
            "capabilities": {"tools_enabled": True, "memory_enabled": False},
            "enabled_tools": ["fake_tool"],
        }
        db.agents.find_one = AsyncMock(return_value=agent_with_tools)

        # First call returns a tool call, second call returns final text
        tc = ToolCall(id="tc1", name="fake_tool", arguments={"input": "test"})
        adapter_with_tool = FakeLLMAdapter(response_text="Let me check.", tool_calls=[tc])
        adapter_final = FakeLLMAdapter(response_text="The answer is 42.")

        call_count = 0

        def get_adapter_side_effect(backend, model):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return adapter_with_tool
            return adapter_final

        mock_llm_mgr.get_adapter.side_effect = get_adapter_side_effect
        mock_llm_mgr.is_backend_healthy = AsyncMock(return_value=True)
        mock_llm_mgr.record_backend_success = AsyncMock()
        mock_llm_mgr.record_backend_failure = AsyncMock()
        mock_llm_mgr.record_fallback = MagicMock()
        mock_hooks.fire = AsyncMock(return_value={})
        mock_steering.drain = MagicMock(return_value=[])

        # Set up tool router with fake tool
        from aria.tools.router import ToolRouter
        from aria.tools.base import ToolResult, ToolStatus
        router = ToolRouter()
        fake = FakeTool(tool_name="fake_tool", result="42")
        router.register_tool(fake)

        orch = _make_orchestrator(db=db, tool_router=router)

        chunks = await _collect_chunks(
            orch.process_message(CONV_ID, "what is the answer?")
        )

        text_content = "".join(c.content for c in chunks if c.type == "text" and c.content)
        assert "42" in text_content
        # Tool call chunk should have been yielded
        assert any(c.type == "tool_call" for c in chunks)

    @pytest.mark.asyncio
    @patch("aria.core.orchestrator.hook_registry")
    @patch("aria.core.orchestrator.llm_manager")
    async def test_think_block_stripping(self, mock_llm_mgr, mock_hooks):
        """<think>reasoning</think>visible text yields only visible text."""
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
        db.agents.find_one = AsyncMock(return_value=DEFAULT_AGENT)
        orch = _make_orchestrator(db=db)

        fake = FakeLLMAdapter(response_text="<think>internal reasoning</think>visible answer")
        mock_llm_mgr.get_adapter.return_value = fake
        mock_llm_mgr.is_backend_healthy = AsyncMock(return_value=True)
        mock_llm_mgr.record_backend_success = AsyncMock()
        mock_llm_mgr.record_backend_failure = AsyncMock()
        mock_llm_mgr.record_fallback = MagicMock()
        mock_hooks.fire = AsyncMock(return_value={})

        chunks = await _collect_chunks(
            orch.process_message(CONV_ID, "hello")
        )

        text_content = "".join(c.content for c in chunks if c.type == "text" and c.content)
        assert "visible answer" in text_content
        assert "internal reasoning" not in text_content
        assert "<think>" not in text_content

    @pytest.mark.asyncio
    @patch("aria.core.orchestrator.hook_registry")
    @patch("aria.core.orchestrator.llm_manager")
    async def test_all_backends_fail(self, mock_llm_mgr, mock_hooks):
        """All candidates fail, yields error chunk."""
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
        db.agents.find_one = AsyncMock(return_value=DEFAULT_AGENT)
        orch = _make_orchestrator(db=db)

        failing_adapter = FakeLLMAdapter(raise_on_call=RuntimeError("LLM down"))
        mock_llm_mgr.get_adapter.return_value = failing_adapter
        mock_llm_mgr.is_backend_healthy = AsyncMock(return_value=True)
        mock_llm_mgr.record_backend_success = AsyncMock()
        mock_llm_mgr.record_backend_failure = AsyncMock()
        mock_llm_mgr.record_fallback = MagicMock()
        mock_hooks.fire = AsyncMock(return_value={})

        chunks = await _collect_chunks(
            orch.process_message(CONV_ID, "hello")
        )

        error_chunks = [c for c in chunks if c.type == "error"]
        assert len(error_chunks) >= 1
        assert "no llm available" in error_chunks[-1].error.lower()

    @pytest.mark.asyncio
    @patch("aria.core.orchestrator.hook_registry")
    @patch("aria.core.orchestrator.llm_manager")
    async def test_fallback_used(self, mock_llm_mgr, mock_hooks):
        """Primary fails, fallback succeeds."""
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
        agent_with_fallback = {
            **DEFAULT_AGENT,
            "fallback_chain": [
                {"backend": "openrouter", "model": "fallback-model", "conditions": {"on_error": True}},
            ],
        }
        db.agents.find_one = AsyncMock(return_value=agent_with_fallback)
        orch = _make_orchestrator(db=db)

        failing = FakeLLMAdapter(raise_on_call=RuntimeError("primary down"))
        success = FakeLLMAdapter(response_text="Fallback response!")

        def adapter_side_effect(backend, model):
            if backend == "llamacpp":
                return failing
            return success

        mock_llm_mgr.get_adapter.side_effect = adapter_side_effect
        mock_llm_mgr.is_backend_healthy = AsyncMock(return_value=True)
        mock_llm_mgr.record_backend_success = AsyncMock()
        mock_llm_mgr.record_backend_failure = AsyncMock()
        mock_llm_mgr.record_fallback = MagicMock()
        mock_hooks.fire = AsyncMock(return_value={})

        chunks = await _collect_chunks(
            orch.process_message(CONV_ID, "hello")
        )

        text_content = "".join(c.content for c in chunks if c.type == "text" and c.content)
        assert "Fallback response!" in text_content
        assert "fallback" in text_content.lower()  # fallback notice emitted
        # Primary failure was recorded
        mock_llm_mgr.record_backend_failure.assert_awaited()

    @pytest.mark.asyncio
    @patch("aria.core.orchestrator.hook_registry")
    @patch("aria.core.orchestrator.llm_manager")
    async def test_memory_extraction_queued(self, mock_llm_mgr, mock_hooks):
        """When auto_extract is True and background_tasks provided, extraction is queued."""
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
        agent_with_memory = {
            **DEFAULT_AGENT,
            "memory_config": {"auto_extract": True},
        }
        db.agents.find_one = AsyncMock(return_value=agent_with_memory)
        orch = _make_orchestrator(db=db)

        fake = FakeLLMAdapter(response_text="Noted.")
        mock_llm_mgr.get_adapter.return_value = fake
        mock_llm_mgr.is_backend_healthy = AsyncMock(return_value=True)
        mock_llm_mgr.record_backend_success = AsyncMock()
        mock_llm_mgr.record_backend_failure = AsyncMock()
        mock_llm_mgr.record_fallback = MagicMock()
        mock_hooks.fire = AsyncMock(return_value={})

        bg_tasks = MagicMock()
        bg_tasks.add_task = MagicMock()

        chunks = await _collect_chunks(
            orch.process_message(CONV_ID, "remember this", background_tasks=bg_tasks)
        )

        assert any(c.type == "done" for c in chunks)
        # background_tasks.add_task should have been called at least once
        # (once for memory extraction, once for summary update)
        assert bg_tasks.add_task.call_count >= 1

    @pytest.mark.asyncio
    @patch("aria.core.orchestrator.hook_registry")
    @patch("aria.core.orchestrator.llm_manager")
    async def test_circuit_breaker_skips_unhealthy(self, mock_llm_mgr, mock_hooks):
        """Unhealthy backends are skipped via circuit breaker check."""
        db = make_mock_db()
        db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
        db.agents.find_one = AsyncMock(return_value=DEFAULT_AGENT)
        orch = _make_orchestrator(db=db)

        # Backend is unhealthy — should be skipped entirely
        mock_llm_mgr.is_backend_healthy = AsyncMock(return_value=False)
        mock_llm_mgr.record_backend_success = AsyncMock()
        mock_llm_mgr.record_backend_failure = AsyncMock()
        mock_llm_mgr.record_fallback = MagicMock()
        mock_hooks.fire = AsyncMock(return_value={})

        chunks = await _collect_chunks(
            orch.process_message(CONV_ID, "hello")
        )

        error_chunks = [c for c in chunks if c.type == "error"]
        assert len(error_chunks) >= 1
        # get_adapter should never have been called
        mock_llm_mgr.get_adapter.assert_not_called()
