"""
ARIA - Anthropic/Claude Adapter

Phase: 4
Purpose: Anthropic Claude API adapter for cloud models

Related Spec Sections:
- Section 6.3: Anthropic Implementation
"""

import json
from typing import AsyncIterator, Optional

from aria.llm.base import LLMAdapter, Message, ToolCall, StreamChunk, Tool

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


class AnthropicAdapter(LLMAdapter):
    """
    Adapter for Anthropic Claude models.

    Supports:
    - Claude 3.5 Sonnet, Claude 3 Opus, Claude 3 Haiku
    - Streaming responses
    - Tool use (function calling)
    - System prompts
    """

    def __init__(self, api_key: str, model: str):
        if not ANTHROPIC_AVAILABLE:
            raise ImportError(
                "anthropic package not installed. "
                "Install with: pip install anthropic"
            )

        self.api_key = api_key
        self.model = model
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    @property
    def name(self) -> str:
        return "anthropic"

    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[Optional[str], list[dict]]:
        """
        Convert ARIA messages to Anthropic format.
        Returns (system_prompt, messages)
        """
        system_prompt = None
        anthropic_messages = []

        for msg in messages:
            if msg.role == "system":
                # Anthropic uses separate system parameter
                system_prompt = msg.content
            elif msg.role == "tool":
                # Tool result
                anthropic_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id,
                            "content": msg.content,
                        }
                    ],
                })
            else:
                # Regular message
                anthropic_messages.append({
                    "role": msg.role,
                    "content": msg.content,
                })

        return system_prompt, anthropic_messages

    def _convert_tools(self, tools: list[Tool]) -> list[dict]:
        """Convert ARIA tools to Anthropic format."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in tools
        ]

    async def stream(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        stream: bool = True,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a completion from Claude."""

        # If non-streaming requested, use complete() and simulate streaming
        if not stream:
            try:
                content, tool_calls_list, usage = await self.complete(
                    messages, tools, temperature, max_tokens
                )
                if content:
                    yield StreamChunk(type="text", content=content)
                for tc in tool_calls_list:
                    yield StreamChunk(type="tool_call", tool_call=tc)
                yield StreamChunk(type="done", usage=usage)
                return
            except Exception as e:
                yield StreamChunk(type="error", error=f"Anthropic error: {str(e)}")
                return

        # Convert messages
        system_prompt, anthropic_messages = self._convert_messages(messages)

        # Build request parameters
        request_params = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if system_prompt:
            request_params["system"] = system_prompt

        if tools:
            request_params["tools"] = self._convert_tools(tools)

        try:
            # Stream the response
            async with self.client.messages.stream(**request_params) as stream_ctx:
                tool_uses = []
                tool_input_buffers = {}  # index -> accumulated JSON string

                async for event in stream_ctx:
                    # Text or tool input delta
                    if event.type == "content_block_delta":
                        if hasattr(event.delta, "text"):
                            yield StreamChunk(
                                type="text",
                                content=event.delta.text,
                            )
                        elif hasattr(event.delta, "partial_json"):
                            # Accumulate tool input JSON fragments
                            if tool_uses:
                                idx = len(tool_uses) - 1
                                tool_input_buffers[idx] = tool_input_buffers.get(idx, "") + event.delta.partial_json

                    # Tool use block start
                    elif event.type == "content_block_start":
                        if hasattr(event.content_block, "type"):
                            if event.content_block.type == "tool_use":
                                tool_uses.append({
                                    "id": event.content_block.id,
                                    "name": event.content_block.name,
                                    "input": {},
                                })

                    # Tool use block end — parse accumulated JSON
                    elif event.type == "content_block_stop":
                        if tool_uses:
                            idx = len(tool_uses) - 1
                            if idx in tool_input_buffers:
                                try:
                                    tool_uses[idx]["input"] = json.loads(tool_input_buffers[idx])
                                except json.JSONDecodeError:
                                    tool_uses[idx]["input"] = {}

                # Get final message for usage stats
                final_message = await stream_ctx.get_final_message()

                # Yield any tool calls
                for tool_use in tool_uses:
                    yield StreamChunk(
                        type="tool_call",
                        tool_call=ToolCall(
                            id=tool_use["id"],
                            name=tool_use["name"],
                            arguments=tool_use["input"],
                        ),
                    )

                # Yield usage stats
                yield StreamChunk(
                    type="done",
                    usage={
                        "input_tokens": final_message.usage.input_tokens,
                        "output_tokens": final_message.usage.output_tokens,
                    },
                )

        except anthropic.APIError as e:
            yield StreamChunk(
                type="error",
                error=f"Anthropic API error: {str(e)}",
            )
        except Exception as e:
            yield StreamChunk(
                type="error",
                error=f"Anthropic error: {str(e)}",
            )

    async def complete(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> tuple[str, list[ToolCall], dict]:
        """Non-streaming completion."""

        content_parts = []
        tool_calls = []
        usage = {}

        async for chunk in self.stream(messages, tools, temperature, max_tokens):
            if chunk.type == "text":
                content_parts.append(chunk.content)
            elif chunk.type == "tool_call":
                tool_calls.append(chunk.tool_call)
            elif chunk.type == "done":
                usage = chunk.usage
            elif chunk.type == "error":
                raise Exception(chunk.error)

        return "".join(content_parts), tool_calls, usage

    async def health_check(self) -> bool:
        """Check if the Anthropic API is accessible."""
        try:
            async for _ in self.stream(
                [Message(role="user", content="hi")],
                max_tokens=10,
            ):
                return True
        except Exception:
            return False
        return False  # empty stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.close()
