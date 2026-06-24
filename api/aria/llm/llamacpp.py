"""
ARIA - llama.cpp Adapter

Phase: 1
Purpose: llama.cpp server adapter (OpenAI-compatible API)

Related Spec Sections:
- Section 6: LLM Adapter Interface
"""

from aria.config import settings
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
        # Explicit timeout: the SDK default (600s) lets a busy/half-open local
        # server hang a caller for ~10min per try. retry_async can't help — a
        # hang never raises. See settings.llamacpp_timeout_seconds.
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=self.api_key,
            timeout=float(settings.llamacpp_timeout_seconds),
        )

    @property
    def name(self) -> str:
        return "llamacpp"
