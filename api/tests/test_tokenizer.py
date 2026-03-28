"""Tests for aria.core.tokenizer — token counting and budget truncation."""

import pytest

from aria.core.tokenizer import (
    count_message_tokens,
    count_tokens,
    get_default_max_context_tokens,
    normalize_model_name,
    truncate_to_budget,
)
from aria.llm.base import Message


# ---------------------------------------------------------------------------
# normalize_model_name
# ---------------------------------------------------------------------------

class TestNormalizeModelName:
    def test_strips_provider_prefix(self):
        assert normalize_model_name("openai/gpt-4o") == "gpt-4o"

    def test_lowercases(self):
        assert normalize_model_name("Claude-3-Opus") == "claude-3-opus"

    def test_strips_whitespace(self):
        assert normalize_model_name("  gpt-4  ") == "gpt-4"

    def test_empty_string(self):
        assert normalize_model_name("") == ""

    def test_none(self):
        assert normalize_model_name(None) == ""

    def test_multiple_slashes(self):
        assert normalize_model_name("provider/org/model") == "org/model"


# ---------------------------------------------------------------------------
# get_default_max_context_tokens
# ---------------------------------------------------------------------------

class TestGetDefaultMaxContextTokens:
    def test_claude_models(self):
        assert get_default_max_context_tokens("claude-3-opus") == 200000

    def test_gpt4o(self):
        assert get_default_max_context_tokens("gpt-4o") == 128000

    def test_gpt4(self):
        assert get_default_max_context_tokens("gpt-4") == 8192

    def test_unknown_model_returns_default(self):
        assert get_default_max_context_tokens("some-random-model") == 32768

    def test_llama_family(self):
        assert get_default_max_context_tokens("llama-3-70b") == 32768

    def test_provider_prefixed(self):
        assert get_default_max_context_tokens("anthropic/claude-3-sonnet") == 200000


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------

class TestCountTokens:
    def test_empty_string(self):
        assert count_tokens("", "gpt-4") == 0

    def test_nonempty_string(self):
        tokens = count_tokens("Hello, world!", "gpt-4")
        assert tokens > 0

    def test_returns_int(self):
        assert isinstance(count_tokens("test", "gpt-4"), int)


# ---------------------------------------------------------------------------
# count_message_tokens
# ---------------------------------------------------------------------------

class TestCountMessageTokens:
    def test_single_message(self):
        msgs = [Message(role="user", content="Hello")]
        tokens = count_message_tokens(msgs, "gpt-4")
        # Should include per-message overhead (4) + role + content + trailing 2
        assert tokens > 0

    def test_empty_list(self):
        assert count_message_tokens([], "gpt-4") == 2  # trailing overhead only

    def test_multiple_messages(self):
        msgs = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hi"),
        ]
        tokens = count_message_tokens(msgs, "gpt-4")
        single = count_message_tokens([msgs[0]], "gpt-4")
        assert tokens > single


# ---------------------------------------------------------------------------
# truncate_to_budget
# ---------------------------------------------------------------------------

class TestTruncateToBudget:
    def test_empty_messages(self):
        assert truncate_to_budget([], 100, "gpt-4") == []

    def test_fits_within_budget(self):
        msgs = [Message(role="user", content="Hi")]
        result = truncate_to_budget(msgs, 100000, "gpt-4")
        assert result == msgs

    def test_preserves_system_message(self):
        msgs = [
            Message(role="system", content="You are a helpful assistant."),
            Message(role="user", content="Message 1"),
            Message(role="user", content="Message 2"),
            Message(role="user", content="Message 3"),
        ]
        budget = count_message_tokens([msgs[0], msgs[-1]], "gpt-4") + 5
        result = truncate_to_budget(msgs, budget, "gpt-4")
        assert result[0].role == "system"
        assert result[-1].content == "Message 3"

    def test_keeps_most_recent_when_no_system(self):
        msgs = [
            Message(role="user", content="Old message " * 50),
            Message(role="user", content="New message"),
        ]
        budget = count_message_tokens([msgs[-1]], "gpt-4") + 5
        result = truncate_to_budget(msgs, budget, "gpt-4")
        assert result[-1].content == "New message"

    def test_zero_budget_returns_something(self):
        msgs = [
            Message(role="system", content="System"),
            Message(role="user", content="User"),
        ]
        result = truncate_to_budget(msgs, 0, "gpt-4")
        assert len(result) >= 1
