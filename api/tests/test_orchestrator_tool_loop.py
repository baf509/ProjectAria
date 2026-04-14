"""Integration tests for the orchestrator tool-call loop.

Verifies the full cycle:
    LLM emits tool_call → orchestrator dispatches to tool_router →
    tool result fed back to LLM → LLM produces final text response.

Tests cover:
  1. Single-round tool call (LLM calls one tool, gets result, responds)
  2. Multi-round tool calls (LLM calls tools in successive rounds)
  3. Multiple tool calls in a single round
  4. Tool error propagation (tool fails, LLM sees the error)
  5. Max tool rounds exhaustion (loop terminates at limit)
  6. Tool output appears correctly in follow-up LLM messages
  7. No tool calls path (plain text response, no loop)
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from aria.llm.base import Message, StreamChunk, Tool, ToolCall
from aria.tools.base import ToolResult, ToolStatus
from aria.tools.router import ToolRouter

from tests.conftest import FakeLLMAdapter, FakeTool, make_mock_db


# ============================================================================
# Helpers
# ============================================================================

def _make_conversation(agent_id, messages=None):
    """Build a minimal conversation document."""
    return {
        "_id": ObjectId(),
        "agent_id": agent_id,
        "active_agent_id": None,
        "title": "Test Conversation",
        "status": "active",
        "messages": messages or [],
        "stats": {"message_count": 0, "total_tokens": 0, "tool_calls": 0},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


def _make_agent(agent_id, tools_enabled=True, enabled_tools=None):
    """Build a minimal agent document."""
    return {
        "_id": agent_id,
        "name": "Test Agent",
        "slug": "test-agent",
        "system_prompt": "You are a helpful assistant.",
        "llm": {
            "backend": "fake",
            "model": "fake-model",
            "temperature": 0.7,
            "max_tokens": 4096,
        },
        "capabilities": {
            "tools_enabled": tools_enabled,
            "memory_enabled": False,
        },
        "enabled_tools": enabled_tools or [],
        "memory_config": {"auto_extract": False},
        "fallback_chain": [],
    }


class MultiRoundFakeLLM(FakeLLMAdapter):
    """
    A fake LLM that can return different responses on successive calls.

    Each entry in `rounds` is a list of StreamChunks to yield for that call.
    After all rounds are consumed, yields a plain text response.
    """

    def __init__(self, rounds: list[list[StreamChunk]]):
        super().__init__()
        self._rounds = list(rounds)
        self._call_count = 0
        self.all_messages_received: list[list[Message]] = []

    async def stream(self, messages, tools=None, temperature=0.7,
                     max_tokens=4096, stream=True) -> AsyncIterator[StreamChunk]:
        self.all_messages_received.append(list(messages))
        self._call_count += 1

        if self._rounds:
            chunks = self._rounds.pop(0)
        else:
            chunks = [
                StreamChunk(type="text", content="Final answer."),
                StreamChunk(type="done", usage={"input_tokens": 5, "output_tokens": 5}),
            ]

        for chunk in chunks:
            yield chunk


def _build_orchestrator(db, tool_router=None):
    """Construct an Orchestrator with mocked dependencies."""
    from aria.core.orchestrator import Orchestrator
    return Orchestrator(
        db=db,
        tool_router=tool_router,
    )


async def _collect_chunks(orchestrator, conversation_id, message):
    """Collect all StreamChunks from process_message."""
    chunks = []
    async for chunk in orchestrator.process_message(conversation_id, message, stream=True):
        chunks.append(chunk)
    return chunks


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def agent_id():
    return ObjectId()


@pytest.fixture
def mock_db(agent_id):
    """Set up a mock DB with conversation and agent documents."""
    db = make_mock_db()
    conv = _make_conversation(agent_id)
    agent = _make_agent(agent_id, tools_enabled=True, enabled_tools=[])

    db.conversations.find_one = AsyncMock(return_value=conv)
    db.agents.find_one = AsyncMock(return_value=agent)

    return db, conv, agent


# ============================================================================
# Tests
# ============================================================================

class TestNoToolCalls:
    """When the LLM returns plain text without tool calls."""

    @pytest.mark.asyncio
    async def test_plain_text_response(self, mock_db, agent_id):
        db, conv, agent = mock_db

        llm = FakeLLMAdapter(response_text="Hello! I'm ARIA.")

        with patch("aria.core.orchestrator.llm_manager") as mock_mgr, \
             patch("aria.core.orchestrator.hook_registry") as mock_hooks, \
             patch("aria.core.orchestrator.steering_queue") as mock_steer:
            mock_mgr.get_adapter.return_value = llm
            mock_mgr.is_backend_healthy = AsyncMock(return_value=True)
            mock_mgr.record_backend_success = AsyncMock()
            mock_mgr.record_fallback = MagicMock()
            mock_hooks.fire = AsyncMock(return_value={})
            mock_steer.drain.return_value = []

            orchestrator = _build_orchestrator(db, tool_router=None)

            # Patch context builder and command router
            orchestrator.context_builder.build_messages = AsyncMock(
                return_value=[Message(role="user", content="Hi")]
            )
            orchestrator.command_router.try_handle = AsyncMock(return_value=None)
            orchestrator.command_router.try_handle_contextual = AsyncMock(return_value=None)

            chunks = await _collect_chunks(orchestrator, str(conv["_id"]), "Hi")

        text_chunks = [c for c in chunks if c.type == "text"]
        done_chunks = [c for c in chunks if c.type == "done"]

        assert len(text_chunks) >= 1
        combined_text = "".join(c.content for c in text_chunks)
        assert "Hello" in combined_text
        assert len(done_chunks) == 1


class TestSingleToolCallRound:
    """LLM requests one tool call, gets result, produces final response."""

    @pytest.mark.asyncio
    async def test_single_tool_call_and_response(self, mock_db, agent_id):
        db, conv, agent = mock_db

        # FakeTool requires an 'input' parameter, so use that in the tool call
        tool_call = ToolCall(id="tc-1", name="web_fetch", arguments={"input": "https://example.com"})

        # Round 1: LLM emits tool call
        round1 = [
            StreamChunk(type="text", content="Let me search for that."),
            StreamChunk(type="tool_call", tool_call=tool_call),
            StreamChunk(type="done", usage={"input_tokens": 10, "output_tokens": 20}),
        ]
        # Round 2: LLM produces final answer
        round2 = [
            StreamChunk(type="text", content="Based on the results, ARIA is great."),
            StreamChunk(type="done", usage={"input_tokens": 15, "output_tokens": 25}),
        ]

        llm = MultiRoundFakeLLM(rounds=[round1, round2])

        # Set up tool router with a real FakeTool
        router = ToolRouter()
        web_tool = FakeTool(tool_name="web_fetch", result={"content": "ARIA documentation page"})
        router.register_tool(web_tool)

        with patch("aria.core.orchestrator.llm_manager") as mock_mgr, \
             patch("aria.core.orchestrator.hook_registry") as mock_hooks, \
             patch("aria.core.orchestrator.steering_queue") as mock_steer, \
             patch("aria.tools.router.settings") as mock_tool_settings:

            mock_mgr.get_adapter.return_value = llm
            mock_mgr.is_backend_healthy = AsyncMock(return_value=True)
            mock_mgr.record_backend_success = AsyncMock()
            mock_mgr.record_fallback = MagicMock()
            mock_hooks.fire = AsyncMock(return_value={})
            mock_steer.drain.return_value = []

            # Tool router settings
            mock_tool_settings.tool_execution_policy = "open"
            mock_tool_settings.tool_allowed_names = []
            mock_tool_settings.tool_denied_names = []
            mock_tool_settings.tool_sensitive_names = []
            mock_tool_settings.tool_rate_limit_per_minute = 60

            orchestrator = _build_orchestrator(db, tool_router=router)
            orchestrator.context_builder.build_messages = AsyncMock(
                return_value=[Message(role="user", content="Tell me about ARIA")]
            )
            orchestrator.command_router.try_handle = AsyncMock(return_value=None)
            orchestrator.command_router.try_handle_contextual = AsyncMock(return_value=None)

            chunks = await _collect_chunks(orchestrator, str(conv["_id"]), "Tell me about ARIA")

        # Verify the tool was actually called
        assert len(web_tool.execute_log) == 1
        assert web_tool.execute_log[0] == {"input": "https://example.com"}

        # Verify text output includes both rounds
        text_chunks = [c for c in chunks if c.type == "text"]
        combined = "".join(c.content for c in text_chunks)
        assert "search" in combined.lower() or "Let me" in combined
        assert "ARIA is great" in combined

        # Verify the tool status was yielded
        assert any("[Tool web_fetch: success]" in c.content for c in text_chunks)

        # Verify the LLM was called twice (round 1 + round 2)
        assert len(llm.all_messages_received) == 2

        # Verify round 2 messages include the tool result
        round2_messages = llm.all_messages_received[1]
        tool_result_msgs = [m for m in round2_messages if m.role == "tool"]
        assert len(tool_result_msgs) == 1
        assert "ARIA documentation page" in tool_result_msgs[0].content

        # Verify assistant message with tool_calls was included
        assistant_with_tools = [
            m for m in round2_messages
            if m.role == "assistant" and m.tool_calls
        ]
        assert len(assistant_with_tools) == 1
        assert assistant_with_tools[0].tool_calls[0]["name"] == "web_fetch"


class TestMultiRoundToolCalls:
    """LLM makes tool calls across multiple rounds."""

    @pytest.mark.asyncio
    async def test_two_rounds_of_tool_calls(self, mock_db, agent_id):
        db, conv, agent = mock_db

        tc1 = ToolCall(id="tc-1", name="shell", arguments={"input": "list files"})
        tc2 = ToolCall(id="tc-2", name="web_fetch", arguments={"input": "check docs"})

        round1 = [
            StreamChunk(type="tool_call", tool_call=tc1),
            StreamChunk(type="done", usage={"input_tokens": 5, "output_tokens": 5}),
        ]
        round2 = [
            StreamChunk(type="tool_call", tool_call=tc2),
            StreamChunk(type="done", usage={"input_tokens": 5, "output_tokens": 5}),
        ]
        round3 = [
            StreamChunk(type="text", content="Done! Found everything."),
            StreamChunk(type="done", usage={"input_tokens": 5, "output_tokens": 5}),
        ]

        llm = MultiRoundFakeLLM(rounds=[round1, round2, round3])

        router = ToolRouter()
        router.register_tool(FakeTool(tool_name="shell", result="file list"))
        router.register_tool(FakeTool(tool_name="web_fetch", result="docs content"))

        with patch("aria.core.orchestrator.llm_manager") as mock_mgr, \
             patch("aria.core.orchestrator.hook_registry") as mock_hooks, \
             patch("aria.core.orchestrator.steering_queue") as mock_steer, \
             patch("aria.tools.router.settings") as mock_tool_settings:

            mock_mgr.get_adapter.return_value = llm
            mock_mgr.is_backend_healthy = AsyncMock(return_value=True)
            mock_mgr.record_backend_success = AsyncMock()
            mock_mgr.record_fallback = MagicMock()
            mock_hooks.fire = AsyncMock(return_value={})
            mock_steer.drain.return_value = []
            mock_tool_settings.tool_execution_policy = "open"
            mock_tool_settings.tool_allowed_names = []
            mock_tool_settings.tool_denied_names = []
            mock_tool_settings.tool_sensitive_names = []
            mock_tool_settings.tool_rate_limit_per_minute = 60

            orchestrator = _build_orchestrator(db, tool_router=router)
            orchestrator.context_builder.build_messages = AsyncMock(
                return_value=[Message(role="user", content="Do the thing")]
            )
            orchestrator.command_router.try_handle = AsyncMock(return_value=None)
            orchestrator.command_router.try_handle_contextual = AsyncMock(return_value=None)

            chunks = await _collect_chunks(orchestrator, str(conv["_id"]), "Do the thing")

        # LLM called 3 times
        assert len(llm.all_messages_received) == 3

        # Round 3 should have tool results from both rounds
        final_msgs = llm.all_messages_received[2]
        tool_msgs = [m for m in final_msgs if m.role == "tool"]
        assert len(tool_msgs) == 2  # one from each round

        # Final text is present
        text_chunks = [c for c in chunks if c.type == "text"]
        combined = "".join(c.content for c in text_chunks)
        assert "Found everything" in combined


class TestMultipleToolCallsInOneRound:
    """LLM requests multiple tools in a single round."""

    @pytest.mark.asyncio
    async def test_parallel_tool_calls(self, mock_db, agent_id):
        db, conv, agent = mock_db

        tc1 = ToolCall(id="tc-a", name="shell", arguments={"input": "cmd1"})
        tc2 = ToolCall(id="tc-b", name="web_fetch", arguments={"input": "url1"})

        round1 = [
            StreamChunk(type="tool_call", tool_call=tc1),
            StreamChunk(type="tool_call", tool_call=tc2),
            StreamChunk(type="done", usage={"input_tokens": 10, "output_tokens": 10}),
        ]
        round2 = [
            StreamChunk(type="text", content="Both tools returned results."),
            StreamChunk(type="done", usage={"input_tokens": 10, "output_tokens": 10}),
        ]

        llm = MultiRoundFakeLLM(rounds=[round1, round2])

        router = ToolRouter()
        shell_tool = FakeTool(tool_name="shell", result="shell output")
        web_tool = FakeTool(tool_name="web_fetch", result="web output")
        router.register_tool(shell_tool)
        router.register_tool(web_tool)

        with patch("aria.core.orchestrator.llm_manager") as mock_mgr, \
             patch("aria.core.orchestrator.hook_registry") as mock_hooks, \
             patch("aria.core.orchestrator.steering_queue") as mock_steer, \
             patch("aria.tools.router.settings") as mock_tool_settings:

            mock_mgr.get_adapter.return_value = llm
            mock_mgr.is_backend_healthy = AsyncMock(return_value=True)
            mock_mgr.record_backend_success = AsyncMock()
            mock_mgr.record_fallback = MagicMock()
            mock_hooks.fire = AsyncMock(return_value={})
            mock_steer.drain.return_value = []
            mock_tool_settings.tool_execution_policy = "open"
            mock_tool_settings.tool_allowed_names = []
            mock_tool_settings.tool_denied_names = []
            mock_tool_settings.tool_sensitive_names = []
            mock_tool_settings.tool_rate_limit_per_minute = 60

            orchestrator = _build_orchestrator(db, tool_router=router)
            orchestrator.context_builder.build_messages = AsyncMock(
                return_value=[Message(role="user", content="Do two things")]
            )
            orchestrator.command_router.try_handle = AsyncMock(return_value=None)
            orchestrator.command_router.try_handle_contextual = AsyncMock(return_value=None)

            chunks = await _collect_chunks(orchestrator, str(conv["_id"]), "Do two things")

        # Both tools were called
        assert len(shell_tool.execute_log) == 1
        assert len(web_tool.execute_log) == 1

        # Round 2 messages include both tool results
        round2_msgs = llm.all_messages_received[1]
        tool_msgs = [m for m in round2_msgs if m.role == "tool"]
        assert len(tool_msgs) == 2

        tool_contents = {m.content for m in tool_msgs}
        assert "shell output" in tool_contents
        assert "web output" in tool_contents


class TestToolErrorPropagation:
    """When a tool call fails, the error is fed back to the LLM."""

    @pytest.mark.asyncio
    async def test_tool_error_fed_to_llm(self, mock_db, agent_id):
        db, conv, agent = mock_db

        tc = ToolCall(id="tc-err", name="failing_tool", arguments={"input": "boom"})

        round1 = [
            StreamChunk(type="tool_call", tool_call=tc),
            StreamChunk(type="done", usage={"input_tokens": 5, "output_tokens": 5}),
        ]
        round2 = [
            StreamChunk(type="text", content="The tool failed, but I can help anyway."),
            StreamChunk(type="done", usage={"input_tokens": 5, "output_tokens": 5}),
        ]

        llm = MultiRoundFakeLLM(rounds=[round1, round2])

        router = ToolRouter()
        failing = FakeTool(
            tool_name="failing_tool",
            raise_on_execute=RuntimeError("database connection lost"),
        )
        router.register_tool(failing)

        with patch("aria.core.orchestrator.llm_manager") as mock_mgr, \
             patch("aria.core.orchestrator.hook_registry") as mock_hooks, \
             patch("aria.core.orchestrator.steering_queue") as mock_steer, \
             patch("aria.tools.router.settings") as mock_tool_settings:

            mock_mgr.get_adapter.return_value = llm
            mock_mgr.is_backend_healthy = AsyncMock(return_value=True)
            mock_mgr.record_backend_success = AsyncMock()
            mock_mgr.record_fallback = MagicMock()
            mock_hooks.fire = AsyncMock(return_value={})
            mock_steer.drain.return_value = []
            mock_tool_settings.tool_execution_policy = "open"
            mock_tool_settings.tool_allowed_names = []
            mock_tool_settings.tool_denied_names = []
            mock_tool_settings.tool_sensitive_names = []
            mock_tool_settings.tool_rate_limit_per_minute = 60

            orchestrator = _build_orchestrator(db, tool_router=router)
            orchestrator.context_builder.build_messages = AsyncMock(
                return_value=[Message(role="user", content="Try the tool")]
            )
            orchestrator.command_router.try_handle = AsyncMock(return_value=None)
            orchestrator.command_router.try_handle_contextual = AsyncMock(return_value=None)

            chunks = await _collect_chunks(orchestrator, str(conv["_id"]), "Try the tool")

        # The error status was yielded to the client
        text_chunks = [c for c in chunks if c.type == "text"]
        combined = "".join(c.content for c in text_chunks)
        assert "error" in combined.lower()

        # The error message was fed back to the LLM in round 2
        round2_msgs = llm.all_messages_received[1]
        tool_msgs = [m for m in round2_msgs if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert "database connection lost" in tool_msgs[0].content

        # LLM handled the error gracefully
        assert "help anyway" in combined


class TestMaxToolRoundsExhaustion:
    """Loop terminates after max_tool_rounds even if LLM keeps requesting tools."""

    @pytest.mark.asyncio
    async def test_max_rounds_terminates(self, mock_db, agent_id):
        db, conv, agent = mock_db

        # Create an LLM that always wants to call a tool
        def make_tool_round(n):
            return [
                StreamChunk(type="text", content=f"Round {n}. "),
                StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(id=f"tc-{n}", name="looper", arguments={"input": f"r{n}"}),
                ),
                StreamChunk(type="done", usage={"input_tokens": 1, "output_tokens": 1}),
            ]

        # 12 rounds: max_tool_rounds=10 means 11 iterations (0..10)
        rounds = [make_tool_round(i) for i in range(12)]
        llm = MultiRoundFakeLLM(rounds=rounds)

        router = ToolRouter()
        router.register_tool(FakeTool(tool_name="looper", result="looped"))

        with patch("aria.core.orchestrator.llm_manager") as mock_mgr, \
             patch("aria.core.orchestrator.hook_registry") as mock_hooks, \
             patch("aria.core.orchestrator.steering_queue") as mock_steer, \
             patch("aria.tools.router.settings") as mock_tool_settings:

            mock_mgr.get_adapter.return_value = llm
            mock_mgr.is_backend_healthy = AsyncMock(return_value=True)
            mock_mgr.record_backend_success = AsyncMock()
            mock_mgr.record_fallback = MagicMock()
            mock_hooks.fire = AsyncMock(return_value={})
            mock_steer.drain.return_value = []
            mock_tool_settings.tool_execution_policy = "open"
            mock_tool_settings.tool_allowed_names = []
            mock_tool_settings.tool_denied_names = []
            mock_tool_settings.tool_sensitive_names = []
            mock_tool_settings.tool_rate_limit_per_minute = 120

            orchestrator = _build_orchestrator(db, tool_router=router)
            orchestrator.context_builder.build_messages = AsyncMock(
                return_value=[Message(role="user", content="Loop forever")]
            )
            orchestrator.command_router.try_handle = AsyncMock(return_value=None)
            orchestrator.command_router.try_handle_contextual = AsyncMock(return_value=None)

            chunks = await _collect_chunks(orchestrator, str(conv["_id"]), "Loop forever")

        # The loop should cap at max_tool_rounds+1 = 11 LLM calls
        assert len(llm.all_messages_received) == 11

        # A done chunk should still be emitted
        done_chunks = [c for c in chunks if c.type == "done"]
        assert len(done_chunks) == 1


class TestToolOutputInLLMMessages:
    """Verify the exact format of tool results passed back to the LLM."""

    @pytest.mark.asyncio
    async def test_tool_result_message_format(self, mock_db, agent_id):
        db, conv, agent = mock_db

        tc = ToolCall(id="tc-fmt", name="web_fetch", arguments={"input": "test"})

        round1 = [
            StreamChunk(type="text", content="Fetching..."),
            StreamChunk(type="tool_call", tool_call=tc),
            StreamChunk(type="done", usage={"input_tokens": 5, "output_tokens": 5}),
        ]
        round2 = [
            StreamChunk(type="text", content="Got it."),
            StreamChunk(type="done", usage={"input_tokens": 5, "output_tokens": 5}),
        ]

        llm = MultiRoundFakeLLM(rounds=[round1, round2])

        router = ToolRouter()
        router.register_tool(FakeTool(tool_name="web_fetch", result={"data": "test result value"}))

        with patch("aria.core.orchestrator.llm_manager") as mock_mgr, \
             patch("aria.core.orchestrator.hook_registry") as mock_hooks, \
             patch("aria.core.orchestrator.steering_queue") as mock_steer, \
             patch("aria.tools.router.settings") as mock_tool_settings:

            mock_mgr.get_adapter.return_value = llm
            mock_mgr.is_backend_healthy = AsyncMock(return_value=True)
            mock_mgr.record_backend_success = AsyncMock()
            mock_mgr.record_fallback = MagicMock()
            mock_hooks.fire = AsyncMock(return_value={})
            mock_steer.drain.return_value = []
            mock_tool_settings.tool_execution_policy = "open"
            mock_tool_settings.tool_allowed_names = []
            mock_tool_settings.tool_denied_names = []
            mock_tool_settings.tool_sensitive_names = []
            mock_tool_settings.tool_rate_limit_per_minute = 60

            orchestrator = _build_orchestrator(db, tool_router=router)
            orchestrator.context_builder.build_messages = AsyncMock(
                return_value=[Message(role="user", content="Fetch it")]
            )
            orchestrator.command_router.try_handle = AsyncMock(return_value=None)
            orchestrator.command_router.try_handle_contextual = AsyncMock(return_value=None)

            chunks = await _collect_chunks(orchestrator, str(conv["_id"]), "Fetch it")

        # Inspect the messages sent to the LLM in round 2
        round2_msgs = llm.all_messages_received[1]

        # Should have: user, assistant (with tool_calls), tool (with result)
        assistant_msgs = [m for m in round2_msgs if m.role == "assistant" and m.tool_calls]
        tool_msgs = [m for m in round2_msgs if m.role == "tool"]

        assert len(assistant_msgs) >= 1
        assert len(tool_msgs) == 1

        # Tool result message format
        tool_msg = tool_msgs[0]
        assert tool_msg.role == "tool"
        assert tool_msg.tool_call_id == "tc-fmt"
        assert "test result value" in tool_msg.content

        # Assistant message includes the tool call reference
        asst = assistant_msgs[-1]
        assert asst.tool_calls[0]["id"] == "tc-fmt"
        assert asst.tool_calls[0]["name"] == "web_fetch"

        # The text content from assistant round 1 is preserved
        assert "Fetching" in asst.content


class TestToolCallWithRealBuiltinThroughRouter:
    """
    End-to-end: orchestrator calls the ShellTool via tool_router.execute_tool
    and the result flows through the full loop.
    """

    @pytest.mark.asyncio
    async def test_real_shell_tool_through_orchestrator(self, mock_db, agent_id):
        db, conv, agent = mock_db

        # LLM asks to run 'echo hello-from-orchestrator'
        tc = ToolCall(id="tc-real", name="shell", arguments={
            "command": "echo hello-from-orchestrator",
        })

        round1 = [
            StreamChunk(type="tool_call", tool_call=tc),
            StreamChunk(type="done", usage={"input_tokens": 5, "output_tokens": 5}),
        ]
        round2 = [
            StreamChunk(type="text", content="Command output received."),
            StreamChunk(type="done", usage={"input_tokens": 5, "output_tokens": 5}),
        ]

        llm = MultiRoundFakeLLM(rounds=[round1, round2])

        # Use a REAL ShellTool through the router
        from aria.tools.builtin.shell import ShellTool
        router = ToolRouter()
        shell_tool = ShellTool()
        router.register_tool(shell_tool)

        with patch("aria.core.orchestrator.llm_manager") as mock_mgr, \
             patch("aria.core.orchestrator.hook_registry") as mock_hooks, \
             patch("aria.core.orchestrator.steering_queue") as mock_steer, \
             patch("aria.tools.router.settings") as mock_tool_settings:

            mock_mgr.get_adapter.return_value = llm
            mock_mgr.is_backend_healthy = AsyncMock(return_value=True)
            mock_mgr.record_backend_success = AsyncMock()
            mock_mgr.record_fallback = MagicMock()
            mock_hooks.fire = AsyncMock(return_value={})
            mock_steer.drain.return_value = []
            mock_tool_settings.tool_execution_policy = "open"
            mock_tool_settings.tool_allowed_names = []
            mock_tool_settings.tool_denied_names = []
            mock_tool_settings.tool_sensitive_names = []
            mock_tool_settings.tool_rate_limit_per_minute = 60

            orchestrator = _build_orchestrator(db, tool_router=router)
            orchestrator.context_builder.build_messages = AsyncMock(
                return_value=[Message(role="user", content="Run a command")]
            )
            orchestrator.command_router.try_handle = AsyncMock(return_value=None)
            orchestrator.command_router.try_handle_contextual = AsyncMock(return_value=None)

            chunks = await _collect_chunks(orchestrator, str(conv["_id"]), "Run a command")

        # The real shell tool ran and produced output
        round2_msgs = llm.all_messages_received[1]
        tool_msgs = [m for m in round2_msgs if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert "hello-from-orchestrator" in tool_msgs[0].content

        # Success status was reported
        text_chunks = [c for c in chunks if c.type == "text"]
        combined = "".join(c.content for c in text_chunks)
        assert "[Tool shell: success]" in combined
