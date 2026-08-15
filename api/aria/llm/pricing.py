"""
ARIA - LLM Pricing

Purpose: Map model ids to $/token so usage can be priced. Local backends are
free; unknown cloud models fall back to a conservative estimate.

EDIT `_PRICES` to match your actual contracts — these are editable estimates,
expressed as ($ per 1M input tokens, $ per 1M output tokens).

Prices refreshed 2026-08-15 against the current Anthropic model table. The
table had gone stale in a way that mattered for the steward's "$ per merged
change" metric: `agents/routing.py` routes to `claude-sonnet-5` and
`claude-opus-4-8`, and NEITHER had an entry — every routed session was priced
at the UNKNOWN_CLOUD guess instead of its real rate.
"""

from __future__ import annotations

from typing import Optional

# Backends whose inference runs on local hardware — always $0.
# "ridge"/"red" are remote from corsair but still Ben's own hardware (Ridge's
# RTX 3090, RED's 5090), so they cost nothing and must not be priced as cloud.
# "pi-code" and the pi provider names ("ds4", "qwen") arrive on usage rows
# written from pi's session JSONL (steward/outcomes.py), where the recorded
# backend is pi's provider, not one of ARIA's LLM adapter names.
LOCAL_BACKENDS = {
    "llamacpp", "agentic", "context1", "ridge", "red", "pi-code", "ds4",
}

# Substrings that identify a locally-served open-weights model by id. A local
# model that reaches pricing with an unrecognised backend (pi records
# `provider: ds4`, `model: DS4-0731-UD-IQ3-S-...`) would otherwise be billed at
# UNKNOWN_CLOUD and quietly invent dollars for work that cost nothing —
# precisely the number the weekly report divides by merged changes.
LOCAL_MODEL_MARKERS = (
    "ds4-", "deepseek-v4", "qwen", "gemma", "laguna", "chadrock", "glm-",
    "llama", "mistral", "ling-", "step-",
)

# Conservative default for a cloud model we don't have an explicit price for.
UNKNOWN_CLOUD = (1.0, 3.0)

# ($/1M input, $/1M output). Estimates — adjust to your contracts.
_PRICES: dict[str, tuple[float, float]] = {
    # --- Anthropic (current) ---
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    # Sonnet 5 carries an introductory rate ($2/$10) through 2026-08-31; the
    # list price is used here so a spend cap never under-counts and then trips
    # late once the intro window closes.
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # --- Anthropic (legacy ids still resolvable) ---
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-0": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-haiku-4": (1.0, 5.0),
    # Fireworks (GLM 5.2) — retired here, priced for historical usage rows
    "accounts/fireworks/models/glm-5p2": (0.55, 2.19),
    # OpenAI
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    # OpenRouter (DeepSeek)
    "deepseek/deepseek-v4-pro": (0.27, 1.10),
    "deepseek/deepseek-v4-flash": (0.07, 0.28),
}

# Longest key first. The fallback loop below matched in dict order, so a dated
# or suffixed id like "claude-opus-4-8-20260101" hit the "claude-opus-4" entry
# and was priced at $15/$75 — 3x its real rate. Specificity, not insertion
# order, has to decide.
_PRICE_KEYS_BY_SPECIFICITY = sorted(_PRICES, key=len, reverse=True)


def is_local_model(model: Optional[str]) -> bool:
    """True when `model` names an open-weights model served on Ben's hardware."""
    if not model:
        return False
    lowered = model.lower()
    return any(marker in lowered for marker in LOCAL_MODEL_MARKERS)


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
    if is_local_model(model):
        return (0.0, 0.0)
    # Prefix / substring match (handles versioned ids like claude-sonnet-5-…),
    # most specific key first.
    for key in _PRICE_KEYS_BY_SPECIFICITY:
        if model.startswith(key) or key in model:
            return _PRICES[key]
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
