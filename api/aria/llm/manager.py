"""
ARIA - LLM Manager

Phase: 1, 4
Purpose: LLM backend selection and management

Related Spec Sections:
- Section 6: LLM Adapter Interface
"""

from aria.llm.base import LLMAdapter
from aria.llm.ollama import OllamaAdapter
from aria.config import settings
import logging

logger = logging.getLogger(__name__)


class LLMManager:
    """Manages LLM backend selection and instantiation."""

    def __init__(self):
        self.adapters = {}

    def get_adapter(self, backend: str, model: str) -> LLMAdapter:
        """
        Get or create an LLM adapter.

        Args:
            backend: Backend name ("ollama", "anthropic", "openai", "openrouter")
            model: Model name

        Returns:
            LLMAdapter instance

        Raises:
            ValueError: If backend is unknown or API key is missing
        """
        key = f"{backend}:{model}"

        if key not in self.adapters:
            if backend == "ollama":
                self.adapters[key] = OllamaAdapter(
                    base_url=settings.ollama_url, model=model
                )
                logger.info(f"Created Ollama adapter for model: {model}")

            elif backend == "anthropic":
                if not settings.anthropic_api_key:
                    raise ValueError(
                        "Anthropic API key not configured. "
                        "Set ANTHROPIC_API_KEY environment variable."
                    )
                try:
                    from aria.llm.anthropic import AnthropicAdapter
                    self.adapters[key] = AnthropicAdapter(
                        api_key=settings.anthropic_api_key, model=model
                    )
                    logger.info(f"Created Anthropic adapter for model: {model}")
                except ImportError:
                    raise ImportError(
                        "anthropic package not installed. "
                        "Install with: pip install anthropic"
                    )

            elif backend == "openai":
                if not settings.openai_api_key:
                    raise ValueError(
                        "OpenAI API key not configured. "
                        "Set OPENAI_API_KEY environment variable."
                    )
                try:
                    from aria.llm.openai import OpenAIAdapter
                    self.adapters[key] = OpenAIAdapter(
                        api_key=settings.openai_api_key, model=model
                    )
                    logger.info(f"Created OpenAI adapter for model: {model}")
                except ImportError:
                    raise ImportError(
                        "openai package not installed. "
                        "Install with: pip install openai"
                    )

            elif backend == "openrouter":
                if not settings.openrouter_api_key:
                    raise ValueError(
                        "OpenRouter API key not configured. "
                        "Set OPENROUTER_API_KEY environment variable."
                    )
                try:
                    from aria.llm.openrouter import OpenRouterAdapter
                    self.adapters[key] = OpenRouterAdapter(
                        api_key=settings.openrouter_api_key, model=model
                    )
                    logger.info(f"Created OpenRouter adapter for model: {model}")
                except ImportError:
                    raise ImportError(
                        "openai package not installed. "
                        "Install with: pip install openai"
                    )

            else:
                raise ValueError(
                    f"Unknown backend: {backend}. "
                    f"Supported: ollama, anthropic, openai, openrouter"
                )

        return self.adapters[key]

    def is_backend_available(self, backend: str) -> tuple[bool, str]:
        """
        Check if a backend is available and configured.

        Returns:
            (is_available, reason)
        """
        if backend == "ollama":
            return True, "Ollama is always available (local)"

        elif backend == "anthropic":
            if not settings.anthropic_api_key:
                return False, "Anthropic API key not configured"
            try:
                import anthropic
                return True, "Anthropic API configured"
            except ImportError:
                return False, "anthropic package not installed"

        elif backend == "openai":
            if not settings.openai_api_key:
                return False, "OpenAI API key not configured"
            try:
                import openai
                return True, "OpenAI API configured"
            except ImportError:
                return False, "openai package not installed"

        elif backend == "openrouter":
            if not settings.openrouter_api_key:
                return False, "OpenRouter API key not configured"
            try:
                import openai
                return True, "OpenRouter API configured"
            except ImportError:
                return False, "openai package not installed (required for OpenRouter)"

        else:
            return False, f"Unknown backend: {backend}"


# Global instance
llm_manager = LLMManager()
