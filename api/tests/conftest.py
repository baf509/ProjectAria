"""
ARIA - Test Fixtures

Shared fixtures for unit and integration tests.
"""

import asyncio
from datetime import datetime
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.llm.base import LLMAdapter, Message, StreamChunk, Tool, ToolCall
from aria.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult, ToolStatus, ToolType
from aria.tools.router import ToolRouter


# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop():
    """Shared event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Fake LLM adapter for deterministic testing
# ---------------------------------------------------------------------------

class FakeLLMAdapter(LLMAdapter):
    """LLM adapter that returns canned responses for testing."""

    def __init__(
        self,
        response_text: str = "Hello from FakeLLM!",
        tool_calls: list[ToolCall] | None = None,
        usage: dict | None = None,
        raise_on_call: Exception | None = None,
    ):
        self.response_text = response_text
        self.fake_tool_calls = tool_calls or []
        self.fake_usage = usage or {"input_tokens": 10, "output_tokens": 20}
        self.raise_on_call = raise_on_call
        self.call_log: list[dict] = []

    @property
    def name(self) -> str:
        return "fake"

    async def stream(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        stream: bool = True,
    ) -> AsyncIterator[StreamChunk]:
        self.call_log.append({
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })

        if self.raise_on_call:
            raise self.raise_on_call

        yield StreamChunk(type="text", content=self.response_text)

        for tc in self.fake_tool_calls:
            yield StreamChunk(type="tool_call", tool_call=tc)

        yield StreamChunk(type="done", usage=self.fake_usage)

    async def complete(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> tuple[str, list[ToolCall], dict]:
        self.call_log.append({
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })

        if self.raise_on_call:
            raise self.raise_on_call

        return self.response_text, self.fake_tool_calls, self.fake_usage


@pytest.fixture
def fake_llm():
    """Create a default FakeLLMAdapter."""
    return FakeLLMAdapter()


# ---------------------------------------------------------------------------
# Fake tool for tool-router testing
# ---------------------------------------------------------------------------

class FakeTool(BaseTool):
    """Minimal tool for testing the tool router."""

    def __init__(
        self,
        tool_name: str = "fake_tool",
        tool_description: str = "A fake tool for tests",
        result: Any = "tool output",
        raise_on_execute: Exception | None = None,
        delay_seconds: float = 0.0,
    ):
        super().__init__()
        self._name = tool_name
        self._description = tool_description
        self._result = result
        self._raise = raise_on_execute
        self._delay = delay_seconds
        self.execute_log: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="input", type="string", description="Test input", required=True),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        self.execute_log.append(arguments)

        if self._delay:
            await asyncio.sleep(self._delay)

        if self._raise:
            raise self._raise

        return ToolResult(
            tool_name=self._name,
            status=ToolStatus.SUCCESS,
            output=self._result,
        )


@pytest.fixture
def fake_tool():
    """Create a default FakeTool."""
    return FakeTool()


@pytest.fixture
def tool_router():
    """Create a fresh ToolRouter."""
    return ToolRouter()


# ---------------------------------------------------------------------------
# Mock MongoDB database
# ---------------------------------------------------------------------------

def make_mock_db() -> MagicMock:
    """Create a MagicMock that mimics motor's AsyncIOMotorDatabase.

    Collections are auto-created on attribute access and have async methods
    pre-configured.
    """
    db = MagicMock()

    def _make_collection(name: str) -> MagicMock:
        coll = MagicMock()
        coll.find_one = AsyncMock(return_value=None)
        coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="mock-id"))
        coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        coll.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))

        # find() returns a mock cursor with sort/limit/to_list
        cursor = MagicMock()
        cursor.sort = MagicMock(return_value=cursor)
        cursor.limit = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=[])
        coll.find = MagicMock(return_value=cursor)

        # aggregate() returns a cursor with to_list
        agg_cursor = MagicMock()
        agg_cursor.to_list = AsyncMock(return_value=[])
        coll.aggregate = MagicMock(return_value=agg_cursor)

        return coll

    # Common collections used throughout the codebase
    for name in [
        "conversations",
        "agents",
        "memories",
        "signal_contacts",
        "usage",
        "research_runs",
        "tasks",
        "audit_log",
        "coding_sessions",
        "workflows",
        "workflow_runs",
    ]:
        setattr(db, name, _make_collection(name))

    return db


@pytest.fixture
def mock_db():
    """Provide a mock MongoDB database."""
    return make_mock_db()
