"""
ARIA - LLM Pricing

Purpose: Map model ids to $/token so usage can be priced. Local backends are
free; unknown cloud models fall back to a conservative estimate.

EDIT `_PRICES` to match your actual contracts — these are editable estimates,
expressed as ($ per 1M input tokens, $ per 1M output tokens).
"""

from __future__ import annotations

from typing import Optional

# Backends whose inference runs on local hardware — always $0.
LOCAL_BACKENDS = {"llamacpp", "agentic", "context1"}

# Conservative default for a cloud model we don't have an explicit price for.
UNKNOWN_CLOUD = (1.0, 3.0)

# ($/1M input, $/1M output). Estimates — adjust to your contracts.
_PRICES: dict[str, tuple[float, float]] = {
    # Fireworks (GLM 5.2) — the orchestrator default
    "accounts/fireworks/models/glm-5p2": (0.55, 2.19),
    # Anthropic
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-haiku-4": (1.0, 5.0),
    # OpenAI
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    # OpenRouter (DeepSeek)
    "deepseek/deepseek-v4-pro": (0.27, 1.10),
    "deepseek/deepseek-v4-flash": (0.07, 0.28),
}


def price_for(model: Optional[str], backend: Optional[str] = None) -> tuple[float, float]:
    """Return ($/1M input, $/1M output) for a model. Local backends are free."""
    if backend in LOCAL_BACKENDS:
        return (0.0, 0.0)
    # "default" is exclusively the local-model alias (llamacpp/agentic/context1)
    # in this codebase, so treat it (and empty) as free even when the historical
    # usage doc has no backend recorded.
    if not model or model == "default":
        return (0.0, 0.0)
    if model in _PRICES:
        return _PRICES[model]
    # Prefix / substring match (handles versioned ids like claude-sonnet-4-…).
    for key, price in _PRICES.items():
        if model.startswith(key) or key in model:
            return price
    return UNKNOWN_CLOUD


def cost_for(
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    backend: Optional[str] = None,
) -> float:
    """Compute the $ cost of a single (input, output) token count."""
    p_in, p_out = price_for(model, backend)
    return (input_tokens / 1_000_000.0) * p_in + (output_tokens / 1_000_000.0) * p_out
