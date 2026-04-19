"""
ARIA - Chroma context-1 Adapter

Purpose: Thin OpenAI-compatible adapter pointing at the second llama.cpp
instance that serves the chromadb/context-1 agentic search model.
"""

from aria.llm.openai import OpenAIAdapter

try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class ContextOneAdapter(OpenAIAdapter):
    """Adapter for the context-1 llama.cpp server (OpenAI-compatible)."""

    def __init__(self, base_url: str, model: str = "default", api_key: str = ""):
        if not OPENAI_AVAILABLE:
            raise ImportError("openai package not installed. Install with: pip install openai")
        self.api_key = api_key or "no-key"
        self.model = model
        self.client = AsyncOpenAI(base_url=base_url, api_key=self.api_key)

    @property
    def name(self) -> str:
        return "context1"
