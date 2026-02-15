"""
ARIA - llama.cpp Adapter

Phase: 1
Purpose: llama.cpp server adapter (OpenAI-compatible API)

Related Spec Sections:
- Section 6: LLM Adapter Interface
"""

from aria.llm.openai import OpenAIAdapter

try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class LlamaCppAdapter(OpenAIAdapter):
    """
    Adapter for llama.cpp server.

    llama.cpp exposes an OpenAI-compatible API, so this is a thin
    wrapper around OpenAIAdapter that points to the local server.
    """

    def __init__(self, base_url: str, model: str = "default", api_key: str = ""):
        if not OPENAI_AVAILABLE:
            raise ImportError(
                "openai package not installed. "
                "Install with: pip install openai"
            )

        self.api_key = api_key or "no-key"
        self.model = model
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=self.api_key,
        )

    @property
    def name(self) -> str:
        return "llamacpp"
