"""
ARIA - Fireworks AI Adapter

Phase: 4
Purpose: Fireworks AI (Firepass) adapter — OpenAI-compatible inference for
GLM 5.2 and other Fireworks-hosted models.

Related Spec Sections:
- Section 6: LLM Adapter Interface

Fireworks exposes an OpenAI-compatible API, so this subclasses OpenRouterAdapter
to reuse its message/tool conversion, streaming, and — importantly — its GLM
reasoning-mode fallback (GLM 5.2 emits reasoning tokens before content). Only the
base URL and adapter name differ.
"""

from aria.llm.openrouter import OpenRouterAdapter

try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class FireworksAdapter(OpenRouterAdapter):
    """Adapter for Fireworks AI (api.fireworks.ai), OpenAI-compatible."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.fireworks.ai/inference/v1",
    ):
        if not OPENAI_AVAILABLE:
            raise ImportError(
                "openai package not installed. Install with: pip install openai"
            )
        self.api_key = api_key
        self.model = model
        self.site_url = None
        self.site_name = None
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    @property
    def name(self) -> str:
        return "fireworks"
