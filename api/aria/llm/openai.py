"""
ARIA - OpenAI Adapter

Phase: 4
Purpose: OpenAI API adapter for cloud models

Related Spec Sections:
- Section 6.4: OpenAI Implementation
"""

import json
from typing import AsyncIterator

from aria.llm.base import LLMAdapter, Message, ToolCall, StreamChunk, Tool

try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class OpenAIAdapter(LLMAdapter):
    """
    Adapter for OpenAI models.

    Supports:
    - GPT-4, GPT-4 Turbo, GPT-3.5 Turbo
    - Streaming responses
    - Function calling (tools)
    - System prompts
    """

    def __init__(self, api_key: str, model: str):
        if not OPENAI_AVAILABLE:
            raise ImportError(
                "openai package not installed. "
                "Install with: pip install openai"
            )

        self.api_key = api_key
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key)

    @property
    def name(self) -> str:
        return "openai"

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """Convert ARIA messages to OpenAI format."""
        openai_messages = []

        for msg in messages:
            openai_msg = {
                "role": msg.role,
                "content": msg.content,
            }

            # Tool result needs special handling
            if msg.role == "tool":
                openai_msg["role"] = "tool"
                openai_msg["tool_call_id"] = msg.tool_call_id
                if msg.name:
                    openai_msg["name"] = msg.name

            openai_messages.append(openai_msg)

        return openai_messages

    def _convert_tools(self, tools: list[Tool]) -> list[dict]:
        """Convert ARIA tools to OpenAI format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
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
        """Stream a completion from OpenAI."""

        # Convert messages and tools
        openai_messages = self._convert_messages(messages)

        # Build request parameters
        request_params = {
            "model": self.model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }

        if tools:
            request_params["tools"] = self._convert_tools(tools)

        try:
            # Stream the response
            stream = await self.client.chat.completions.create(**request_params)

            tool_calls_accumulator = {}

            async for chunk in stream:
                delta = chunk.choices[0].delta

                # Text content
                if delta.content:
                    yield StreamChunk(
                        type="text",
                        content=delta.content,
                    )

                # Tool calls
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index

                        # Initialize or update tool call
                        if idx not in tool_calls_accumulator:
                            tool_calls_accumulator[idx] = {
                                "id": tc_delta.id or "",
                                "name": "",
                                "arguments": "",
                            }

                        if tc_delta.id:
                            tool_calls_accumulator[idx]["id"] = tc_delta.id

                        if tc_delta.function:
                            if tc_delta.function.name:
                                tool_calls_accumulator[idx]["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                tool_calls_accumulator[idx]["arguments"] += tc_delta.function.arguments

                # Check if done
                if chunk.choices[0].finish_reason:
                    # Yield completed tool calls
                    for tool_call_data in tool_calls_accumulator.values():
                        try:
                            arguments = json.loads(tool_call_data["arguments"])
                        except json.JSONDecodeError:
                            arguments = {}

                        yield StreamChunk(
                            type="tool_call",
                            tool_call=ToolCall(
                                id=tool_call_data["id"],
                                name=tool_call_data["name"],
                                arguments=arguments,
                            ),
                        )

                    # Yield usage if available
                    usage = {}
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage = {
                            "input_tokens": chunk.usage.prompt_tokens,
                            "output_tokens": chunk.usage.completion_tokens,
                        }

                    yield StreamChunk(
                        type="done",
                        usage=usage,
                    )

        except Exception as e:
            yield StreamChunk(
                type="error",
                error=f"OpenAI error: {str(e)}",
            )

    async def complete(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        stream: bool = True,
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
        """Check if the OpenAI API is accessible."""
        try:
            # Try a minimal completion
            async for _ in self.stream(
                [Message(role="user", content="hi")],
                max_tokens=10,
            ):
                return True
        except Exception:
            return False
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.close()
