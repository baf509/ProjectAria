"""
ARIA - LLM Manager

Phase: 1, 4
Purpose: LLM backend selection and management

Related Spec Sections:
- Section 6: LLM Adapter Interface
"""

from aria.llm.base import LLMAdapter
from aria.config import settings
from aria.core.resilience import CircuitBreaker
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


class LLMManager:
    """Manages LLM backend selection and instantiation."""

    def __init__(self):
        self.adapters = {}
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        # Fallback telemetry counters
        self._fallback_counts: dict[str, int] = defaultdict(int)
        self._success_counts: dict[str, int] = defaultdict(int)
        self._failure_counts: dict[str, int] = defaultdict(int)

    def get_circuit_breaker(self, backend: str) -> CircuitBreaker:
        """Get or create a circuit breaker for a backend."""
        if backend not in self._circuit_breakers:
            self._circuit_breakers[backend] = CircuitBreaker(
                failure_threshold=5,
                recovery_timeout_seconds=60,
            )
        return self._circuit_breakers[backend]

    async def is_backend_healthy(self, backend: str) -> bool:
        """Check if a backend's circuit breaker allows requests."""
        cb = self.get_circuit_breaker(backend)
        return await cb.allow_request()

    async def record_backend_success(self, backend: str) -> None:
        """Record a successful call to a backend."""
        cb = self.get_circuit_breaker(backend)
        await cb.record_success()
        self._success_counts[backend] += 1

    async def record_backend_failure(self, backend: str) -> None:
        """Record a failed call to a backend."""
        cb = self.get_circuit_breaker(backend)
        await cb.record_failure()
        self._failure_counts[backend] += 1

    def record_fallback(self, from_backend: str, to_backend: str) -> None:
        """Record a fallback activation between backends."""
        key = f"{from_backend}->{to_backend}"
        self._fallback_counts[key] += 1
        logger.warning(
            "LLM fallback activated: %s -> %s (total: %d)",
            from_backend, to_backend, self._fallback_counts[key],
        )

    def get_telemetry(self) -> dict:
        """Get telemetry data for all backends."""
        return {
            "fallbacks": dict(self._fallback_counts),
            "successes": dict(self._success_counts),
            "failures": dict(self._failure_counts),
        }

    def get_adapter(self, backend: str, model: str) -> LLMAdapter:
        """
        Get or create an LLM adapter.

        Args:
            backend: Backend name ("llamacpp", "anthropic", "openai", "openrouter")
            model: Model name

        Returns:
            LLMAdapter instance

        Raises:
            ValueError: If backend is unknown or API key is missing
        """
        key = f"{backend}:{model}"

        if key not in self.adapters:
            if backend == "llamacpp":
                try:
                    from aria.llm.llamacpp import LlamaCppAdapter
                    self.adapters[key] = LlamaCppAdapter(
                        base_url=settings.llamacpp_url,
                        model=model,
                        api_key=settings.llamacpp_api_key,
                    )
                    logger.info(f"Created llama.cpp adapter for model: {model}")
                except ImportError:
                    raise ImportError(
                        "openai package not installed. "
                        "Install with: pip install openai"
                    )

            elif backend == "context1":
                try:
                    from aria.llm.context1 import ContextOneAdapter
                    self.adapters[key] = ContextOneAdapter(
                        base_url=settings.context1_url,
                        model=model,
                        api_key=settings.context1_api_key,
                    )
                    logger.info(f"Created context-1 adapter for model: {model}")
                except ImportError:
                    raise ImportError(
                        "openai package not installed. Install with: pip install openai"
                    )

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
                    f"Supported: llamacpp, context1, anthropic, openai, openrouter"
                )

        return self.adapters[key]

    async def close_all(self):
        """Close all adapter HTTP clients for clean shutdown."""
        for key, adapter in list(self.adapters.items()):
            try:
                if hasattr(adapter, '__aexit__'):
                    await adapter.__aexit__(None, None, None)
                elif hasattr(adapter, 'client') and hasattr(adapter.client, 'close'):
                    await adapter.client.close()
            except Exception as e:
                logger.warning("Error closing adapter %s: %s", key, e)
        self.adapters.clear()

    def is_backend_available(self, backend: str) -> tuple[bool, str]:
        """
        Check if a backend is available and configured.

        Returns:
            (is_available, reason)
        """
        if backend == "llamacpp":
            try:
                import openai
                return True, "llama.cpp is available (local)"
            except ImportError:
                return False, "openai package not installed (required for llama.cpp)"

        elif backend == "context1":
            try:
                import openai
                return True, "context-1 is available (local)"
            except ImportError:
                return False, "openai package not installed (required for context-1)"

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
