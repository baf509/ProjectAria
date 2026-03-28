"""
ARIA - Tokenizer Utilities

Purpose: Token counting and budget-aware truncation.
"""

from __future__ import annotations

from functools import lru_cache

from aria.llm.base import Message

try:
    import tiktoken

    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False


MODEL_CONTEXT_WINDOWS = {
    "gpt-4o": 128000,
    "gpt-4.1": 1047576,
    "gpt-4-turbo": 128000,
    "gpt-4": 8192,
    "gpt-3.5-turbo": 16385,
    "claude-3-7-sonnet": 200000,
    "claude-3-5-sonnet": 200000,
    "claude-3-5-haiku": 200000,
    "claude-3-opus": 200000,
    "claude-3-sonnet": 200000,
    "claude-3-haiku": 200000,
}

MODEL_ENCODING_OVERRIDES = {
    "claude": "cl100k_base",
    "llama": "cl100k_base",
    "mistral": "cl100k_base",
    "gemma": "cl100k_base",
    "qwen": "cl100k_base",
    "deepseek": "cl100k_base",
}


def normalize_model_name(model: str) -> str:
    """Normalize provider-qualified model names for lookup."""
    normalized = (model or "").strip().lower()
    if "/" in normalized:
        normalized = normalized.split("/", 1)[1]
    return normalized


def get_default_max_context_tokens(model: str) -> int:
    """Return a sensible default context window for the given model."""
    normalized = normalize_model_name(model)

    for prefix, context_window in MODEL_CONTEXT_WINDOWS.items():
        if normalized.startswith(prefix):
            return context_window

    if normalized.startswith("claude"):
        return 200000
    if normalized.startswith(("gpt", "o1", "o3")):
        return 128000
    if normalized.startswith(("llama", "mistral", "qwen", "gemma", "deepseek")):
        return 32768

    return 32768


@lru_cache(maxsize=32)
def get_encoding(model: str):
    """Get a cached tokenizer encoding for a model family."""
    if not TIKTOKEN_AVAILABLE:
        return None

    normalized = normalize_model_name(model)

    try:
        return tiktoken.encoding_for_model(normalized)
    except KeyError:
        pass

    for prefix, encoding_name in MODEL_ENCODING_OVERRIDES.items():
        if normalized.startswith(prefix):
            return tiktoken.get_encoding(encoding_name)

    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str, model: str) -> int:
    """Count tokens in a text string, falling back to a coarse heuristic."""
    if not text:
        return 0

    encoding = get_encoding(model)
    if encoding is None:
        return max(1, len(text) // 4)

    return len(encoding.encode(text))


def count_message_tokens(messages: list[Message], model: str) -> int:
    """Approximate total chat tokens including per-message overhead."""
    total = 0
    for msg in messages:
        total += 4
        total += count_tokens(msg.role, model)
        total += count_tokens(msg.content, model)
        if msg.name:
            total += count_tokens(msg.name, model)
        if msg.tool_call_id:
            total += count_tokens(msg.tool_call_id, model)

    return total + 2


def truncate_to_budget(messages: list[Message], budget: int, model: str) -> list[Message]:
    """
    Truncate messages to fit within the token budget.

    Preserves the first system message when present and keeps the most recent
    non-system messages that fit in the remaining budget.
    """
    if not messages:
        return []

    if budget <= 0:
        return messages[:1] if messages[0].role == "system" else messages[-1:]

    if count_message_tokens(messages, model) <= budget:
        return messages

    preserved_prefix: list[Message] = []
    remaining_messages = messages

    if messages[0].role == "system":
        preserved_prefix = [messages[0]]
        remaining_messages = messages[1:]

    trimmed_reversed: list[Message] = []
    running_total = count_message_tokens(preserved_prefix, model) if preserved_prefix else 0

    for msg in reversed(remaining_messages):
        msg_tokens = count_message_tokens([msg], model)
        if trimmed_reversed and running_total + msg_tokens > budget:
            break
        if not trimmed_reversed and preserved_prefix and running_total + msg_tokens > budget:
            continue
        if not trimmed_reversed and not preserved_prefix and msg_tokens > budget:
            return [msg]

        trimmed_reversed.append(msg)
        running_total += msg_tokens

    return preserved_prefix + list(reversed(trimmed_reversed))
