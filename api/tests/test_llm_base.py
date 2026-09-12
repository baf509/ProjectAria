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


# ---------------------------------------------------------------------------
# Adapter interface conformance
# ---------------------------------------------------------------------------

class TestAdapterSignatureContract:
    """Every concrete adapter must accept the full base signature.

    The orchestrator resolves an adapter from configuration and calls it with a
    fixed keyword set, so a parameter added to one implementation and not the
    others is a TypeError waiting on whichever backend an agent happens to be
    pointed at. `agent_slug` was added to OpenAIAdapter alone and shipped green,
    because the FakeLLMAdapter the suite exercises had been updated too — the
    real adapters were never checked against each other.
    """

    @staticmethod
    def _concrete_adapters():
        from aria.llm.anthropic import AnthropicAdapter
        from aria.llm.llamacpp import LlamaCppAdapter
        from aria.llm.openai import OpenAIAdapter
        from aria.llm.openrouter import OpenRouterAdapter

        return [AnthropicAdapter, LlamaCppAdapter, OpenAIAdapter, OpenRouterAdapter,
                FakeLLMAdapter]

    @pytest.mark.parametrize("method", ["stream", "complete"])
    def test_every_adapter_accepts_the_base_keywords(self, method):
        import inspect

        from aria.llm.base import LLMAdapter

        base = inspect.signature(getattr(LLMAdapter, method)).parameters
        required = {
            name for name, p in base.items()
            if name != "self" and p.kind is not p.VAR_KEYWORD
        }
        for cls in self._concrete_adapters():
            params = inspect.signature(getattr(cls, method)).parameters
            if any(p.kind is p.VAR_KEYWORD for p in params.values()):
                continue  # **kwargs absorbs anything the base adds
            missing = required - set(params)
            assert not missing, (
                f"{cls.__name__}.{method} is missing {sorted(missing)} from the "
                f"LLMAdapter contract; the orchestrator passes them positionally "
                f"by name to whichever adapter configuration resolved"
            )
