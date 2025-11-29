"""
ARIA - LLM Adapter Base

Phase: 1
Purpose: Abstract base class for LLM adapters

Related Spec Sections:
- Section 6.1: Base Interface
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator
from dataclasses import dataclass


@dataclass
class Message:
    """Message in conversation."""
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: str = None  # For tool results
    name: str = None  # Tool name for tool results


@dataclass
class ToolCall:
    """Tool call from LLM."""
    id: str
    name: str
    arguments: dict


@dataclass
class StreamChunk:
    """Chunk of streaming response."""
    type: str  # "text" | "tool_call" | "tool_call_delta" | "done" | "error"
    content: str = None
    tool_call: ToolCall = None
    usage: dict = None
    error: str = None

    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        result = {"type": self.type}
        if self.content is not None:
            result["content"] = self.content
        if self.tool_call is not None:
            result["tool_call"] = {
                "id": self.tool_call.id,
                "name": self.tool_call.name,
                "arguments": self.tool_call.arguments,
            }
        if self.usage is not None:
            result["usage"] = self.usage
        if self.error is not None:
            result["error"] = self.error
        return result


@dataclass
class Tool:
    """Tool definition for LLM."""
    name: str
    description: str
    parameters: dict  # JSON Schema


class LLMAdapter(ABC):
    """
    Abstract base class for all LLM backends.
    Implement this interface to add a new LLM provider.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend name, e.g., 'ollama', 'anthropic'."""
        pass

    @abstractmethod
    async def stream(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        """
        Stream a completion with optional tool use.
        Yields StreamChunk objects.
        """
        pass

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> tuple[str, list[ToolCall], dict]:
        """
        Non-streaming completion.
        Returns (content, tool_calls, usage).
        """
        pass

    async def health_check(self) -> bool:
        """Check if the backend is available."""
        try:
            # Try a minimal completion
            async for _ in self.stream([Message(role="user", content="hi")]):
                return True
        except Exception:
            return False
        return False
