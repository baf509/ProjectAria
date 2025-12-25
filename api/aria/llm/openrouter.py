"""
ARIA - OpenRouter Adapter

Phase: 4
Purpose: OpenRouter API adapter for accessing multiple LLM providers

Related Spec Sections:
- Section 6: LLM Adapter Interface
"""

import json
from typing import AsyncIterator

from aria.llm.base import LLMAdapter, Message, ToolCall, StreamChunk, Tool

try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class OpenRouterAdapter(LLMAdapter):
    """
    Adapter for OpenRouter - unified API for multiple LLM providers.

    OpenRouter is OpenAI-compatible and provides access to models from:
    - OpenAI (GPT-4, GPT-3.5)
    - Anthropic (Claude)
    - Google (Gemini)
    - Meta (Llama)
    - And many more providers

    Supports:
    - Streaming responses
    - Function calling (tools)
    - System prompts
    """

    def __init__(self, api_key: str, model: str, site_url: str = None, site_name: str = None):
        if not OPENAI_AVAILABLE:
            raise ImportError(
                "openai package not installed. "
                "Install with: pip install openai"
            )

        self.api_key = api_key
        self.model = model
        self.site_url = site_url
        self.site_name = site_name

        # Configure OpenAI client with OpenRouter base URL
        self.client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            default_headers={
                **({"HTTP-Referer": site_url} if site_url else {}),
                **({"X-Title": site_name} if site_name else {}),
            }
        )

    @property
    def name(self) -> str:
        return "openrouter"

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """Convert ARIA messages to OpenRouter/OpenAI format."""
        openrouter_messages = []

        for msg in messages:
            openrouter_msg = {
                "role": msg.role,
                "content": msg.content,
            }

            # Tool result needs special handling
            if msg.role == "tool":
                openrouter_msg["role"] = "tool"
                openrouter_msg["tool_call_id"] = msg.tool_call_id
                if msg.name:
                    openrouter_msg["name"] = msg.name

            openrouter_messages.append(openrouter_msg)

        return openrouter_messages

    def _convert_tools(self, tools: list[Tool]) -> list[dict]:
        """Convert ARIA tools to OpenRouter/OpenAI format."""
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
    ) -> AsyncIterator[StreamChunk]:
        """Stream a completion from OpenRouter."""

        # Convert messages and tools
        openrouter_messages = self._convert_messages(messages)

        # Build request parameters
        request_params = {
            "model": self.model,
            "messages": openrouter_messages,
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
                error=f"OpenRouter error: {str(e)}",
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
        """Check if the OpenRouter API is accessible."""
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
