"""
ARIA - Ollama Adapter

Phase: 1
Purpose: Ollama LLM adapter for local models

Related Spec Sections:
- Section 6.2: Ollama Implementation
"""

import json
import uuid
from typing import AsyncIterator

import httpx

from aria.llm.base import LLMAdapter, Message, ToolCall, StreamChunk, Tool


class OllamaAdapter(LLMAdapter):
    """
    Adapter for Ollama local models.
    """

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(timeout=120.0)

    @property
    def name(self) -> str:
        return "ollama"

    async def stream(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:

        # Convert messages to Ollama format
        ollama_messages = []
        for msg in messages:
            ollama_msg = {"role": msg.role, "content": msg.content}
            if msg.tool_call_id:
                # Tool result
                ollama_msg["role"] = "tool"
                ollama_msg["tool_call_id"] = msg.tool_call_id
            ollama_messages.append(ollama_msg)

        # Convert tools to Ollama format
        ollama_tools = None
        if tools:
            ollama_tools = [
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

        request_body = {
            "model": self.model,
            "messages": ollama_messages,
            "stream": True,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }

        if ollama_tools:
            request_body["tools"] = ollama_tools

        try:
            async with self.client.stream(
                "POST", f"{self.base_url}/api/chat", json=request_body
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("done"):
                        yield StreamChunk(
                            type="done",
                            usage={
                                "input_tokens": data.get("prompt_eval_count", 0),
                                "output_tokens": data.get("eval_count", 0),
                            },
                        )
                        return

                    message = data.get("message", {})

                    # Text content
                    if message.get("content"):
                        yield StreamChunk(type="text", content=message["content"])

                    # Tool calls
                    if message.get("tool_calls"):
                        for tc in message["tool_calls"]:
                            yield StreamChunk(
                                type="tool_call",
                                tool_call=ToolCall(
                                    id=tc.get("id", str(uuid.uuid4())),
                                    name=tc["function"]["name"],
                                    arguments=json.loads(tc["function"]["arguments"]),
                                ),
                            )
        except httpx.HTTPStatusError as e:
            yield StreamChunk(
                type="error", error=f"Ollama HTTP error: {e.response.status_code}"
            )
        except Exception as e:
            yield StreamChunk(type="error", error=f"Ollama error: {str(e)}")

    async def complete(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> tuple[str, list[ToolCall], dict]:

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

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()
