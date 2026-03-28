"""Tests for aria.llm.base — data classes and adapter interface."""

import pytest

from aria.llm.base import Message, StreamChunk, Tool, ToolCall

from tests.conftest import FakeLLMAdapter


# ---------------------------------------------------------------------------
# StreamChunk serialization
# ---------------------------------------------------------------------------

class TestStreamChunk:
    def test_text_chunk_to_dict(self):
        chunk = StreamChunk(type="text", content="hello")
        d = chunk.to_dict()
        assert d == {"type": "text", "content": "hello"}

    def test_tool_call_chunk_to_dict(self):
        tc = ToolCall(id="tc1", name="web", arguments={"url": "https://x.com"})
        chunk = StreamChunk(type="tool_call", tool_call=tc)
        d = chunk.to_dict()
        assert d["type"] == "tool_call"
        assert d["tool_call"]["id"] == "tc1"
        assert d["tool_call"]["name"] == "web"

    def test_done_chunk_to_dict(self):
        chunk = StreamChunk(type="done", usage={"input_tokens": 5})
        d = chunk.to_dict()
        assert d["type"] == "done"
        assert d["usage"]["input_tokens"] == 5

    def test_error_chunk_to_dict(self):
        chunk = StreamChunk(type="error", error="something broke")
        d = chunk.to_dict()
        assert d["type"] == "error"
        assert d["error"] == "something broke"

    def test_minimal_chunk_omits_none_fields(self):
        chunk = StreamChunk(type="text")
        d = chunk.to_dict()
        assert "content" not in d
        assert "tool_call" not in d
        assert "usage" not in d
        assert "error" not in d


# ---------------------------------------------------------------------------
# FakeLLMAdapter
# ---------------------------------------------------------------------------

class TestFakeLLMAdapter:
    @pytest.mark.asyncio
    async def test_stream_returns_text_and_done(self, fake_llm):
        chunks = []
        async for chunk in fake_llm.stream([Message(role="user", content="hi")]):
            chunks.append(chunk)

        types = [c.type for c in chunks]
        assert "text" in types
        assert "done" in types

    @pytest.mark.asyncio
    async def test_stream_logs_calls(self, fake_llm):
        async for _ in fake_llm.stream([Message(role="user", content="hi")]):
            pass
        assert len(fake_llm.call_log) == 1
        assert fake_llm.call_log[0]["messages"][0].content == "hi"

    @pytest.mark.asyncio
    async def test_stream_raises_when_configured(self):
        llm = FakeLLMAdapter(raise_on_call=RuntimeError("fail"))
        with pytest.raises(RuntimeError, match="fail"):
            async for _ in llm.stream([Message(role="user", content="hi")]):
                pass

    @pytest.mark.asyncio
    async def test_complete_returns_tuple(self, fake_llm):
        content, tool_calls, usage = await fake_llm.complete(
            [Message(role="user", content="hi")]
        )
        assert content == "Hello from FakeLLM!"
        assert tool_calls == []
        assert "input_tokens" in usage

    @pytest.mark.asyncio
    async def test_stream_with_tool_calls(self):
        tc = ToolCall(id="tc1", name="web", arguments={})
        llm = FakeLLMAdapter(tool_calls=[tc])
        chunks = []
        async for chunk in llm.stream([Message(role="user", content="hi")]):
            chunks.append(chunk)

        tool_chunks = [c for c in chunks if c.type == "tool_call"]
        assert len(tool_chunks) == 1
        assert tool_chunks[0].tool_call.name == "web"

    def test_adapter_name(self, fake_llm):
        assert fake_llm.name == "fake"
