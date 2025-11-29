"""
ARIA - LLM Manager

Phase: 1
Purpose: LLM backend selection and management

Related Spec Sections:
- Section 6: LLM Adapter Interface
"""

from aria.llm.base import LLMAdapter
from aria.llm.ollama import OllamaAdapter
from aria.config import settings


class LLMManager:
    """Manages LLM backend selection and instantiation."""

    def __init__(self):
        self.adapters = {}

    def get_adapter(self, backend: str, model: str) -> LLMAdapter:
        """
        Get or create an LLM adapter.

        Args:
            backend: Backend name ("ollama", "anthropic", "openai")
            model: Model name

        Returns:
            LLMAdapter instance
        """
        key = f"{backend}:{model}"

        if key not in self.adapters:
            if backend == "ollama":
                self.adapters[key] = OllamaAdapter(
                    base_url=settings.ollama_url, model=model
                )
            elif backend == "anthropic":
                # TODO: Implement in Phase 4
                raise NotImplementedError("Anthropic adapter not yet implemented")
            elif backend == "openai":
                # TODO: Implement in Phase 4
                raise NotImplementedError("OpenAI adapter not yet implemented")
            else:
                raise ValueError(f"Unknown backend: {backend}")

        return self.adapters[key]


# Global instance
llm_manager = LLMManager()
