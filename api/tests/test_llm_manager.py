"""
Tests for aria.llm.manager.LLMManager

Covers circuit breakers, telemetry, adapter creation/caching,
backend availability checks, and shutdown.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aria.llm.manager import LLMManager


@pytest.fixture
def manager():
    """Fresh LLMManager for each test."""
    return LLMManager()


# ---------------------------------------------------------------------------
# Circuit breaker tests
# ---------------------------------------------------------------------------


def test_get_circuit_breaker_creates_new(manager):
    cb = manager.get_circuit_breaker("llamacpp")
    assert cb is not None
    assert "llamacpp" in manager._circuit_breakers


def test_get_circuit_breaker_reuses_existing(manager):
    cb1 = manager.get_circuit_breaker("llamacpp")
    cb2 = manager.get_circuit_breaker("llamacpp")
    assert cb1 is cb2


@pytest.mark.asyncio
async def test_is_backend_healthy_new_backend(manager):
    healthy = await manager.is_backend_healthy("llamacpp")
    assert healthy is True


# ---------------------------------------------------------------------------
# Telemetry tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_backend_success(manager):
    await manager.record_backend_success("anthropic")
    assert manager._success_counts["anthropic"] == 1
    await manager.record_backend_success("anthropic")
    assert manager._success_counts["anthropic"] == 2


@pytest.mark.asyncio
async def test_record_backend_failure(manager):
    await manager.record_backend_failure("openai")
    assert manager._failure_counts["openai"] == 1
    await manager.record_backend_failure("openai")
    assert manager._failure_counts["openai"] == 2


def test_record_fallback(manager):
    manager.record_fallback("llamacpp", "anthropic")
    assert manager._fallback_counts["llamacpp->anthropic"] == 1
    manager.record_fallback("llamacpp", "anthropic")
    assert manager._fallback_counts["llamacpp->anthropic"] == 2


@pytest.mark.asyncio
async def test_get_telemetry(manager):
    await manager.record_backend_success("llamacpp")
    await manager.record_backend_failure("openai")
    manager.record_fallback("openai", "anthropic")

    telemetry = manager.get_telemetry()
    assert "fallbacks" in telemetry
    assert "successes" in telemetry
    assert "failures" in telemetry
    assert telemetry["successes"]["llamacpp"] == 1
    assert telemetry["failures"]["openai"] == 1
    assert telemetry["fallbacks"]["openai->anthropic"] == 1


# ---------------------------------------------------------------------------
# get_adapter tests
# ---------------------------------------------------------------------------


def test_get_adapter_unknown_backend(manager):
    with pytest.raises(ValueError, match="Unknown backend"):
        manager.get_adapter("foobar", "some-model")


@patch("aria.llm.manager.settings")
def test_get_adapter_anthropic_no_key(mock_settings, manager):
    mock_settings.anthropic_api_key = ""
    with pytest.raises(ValueError, match="Anthropic API key not configured"):
        manager.get_adapter("anthropic", "claude-3")


@patch("aria.llm.manager.settings")
def test_get_adapter_openai_no_key(mock_settings, manager):
    mock_settings.openai_api_key = ""
    with pytest.raises(ValueError, match="OpenAI API key not configured"):
        manager.get_adapter("openai", "gpt-4")


@patch("aria.llm.manager.settings")
def test_get_adapter_openrouter_no_key(mock_settings, manager):
    mock_settings.openrouter_api_key = ""
    with pytest.raises(ValueError, match="OpenRouter API key not configured"):
        manager.get_adapter("openrouter", "meta/llama-3")


def test_get_adapter_caches(manager):
    """Pre-populate the cache and verify second call returns the same object."""
    fake_adapter = MagicMock()
    manager.adapters["llamacpp:my-model"] = fake_adapter

    result = manager.get_adapter("llamacpp", "my-model")
    assert result is fake_adapter


@patch("aria.llm.manager.settings")
def test_get_adapter_llamacpp(mock_settings, manager):
    mock_settings.llamacpp_url = "http://localhost:8080/v1"
    mock_settings.llamacpp_api_key = ""

    fake_adapter = MagicMock()
    fake_module = MagicMock()
    fake_module.LlamaCppAdapter.return_value = fake_adapter

    with patch.dict("sys.modules", {"aria.llm.llamacpp": fake_module}):
        adapter = manager.get_adapter("llamacpp", "local-model")

    assert adapter is fake_adapter
    fake_module.LlamaCppAdapter.assert_called_once_with(
        base_url="http://localhost:8080/v1",
        model="local-model",
        api_key="",
    )


@patch("aria.llm.manager.settings")
def test_get_adapter_anthropic(mock_settings, manager):
    mock_settings.anthropic_api_key = "sk-ant-test"

    fake_adapter = MagicMock()
    fake_module = MagicMock()
    fake_module.AnthropicAdapter.return_value = fake_adapter

    with patch.dict("sys.modules", {"aria.llm.anthropic": fake_module}):
        adapter = manager.get_adapter("anthropic", "claude-3")

    assert adapter is fake_adapter
    fake_module.AnthropicAdapter.assert_called_once_with(
        api_key="sk-ant-test", model="claude-3"
    )


@patch("aria.llm.manager.settings")
def test_get_adapter_openai(mock_settings, manager):
    mock_settings.openai_api_key = "sk-test"

    fake_adapter = MagicMock()
    fake_module = MagicMock()
    fake_module.OpenAIAdapter.return_value = fake_adapter

    with patch.dict("sys.modules", {"aria.llm.openai": fake_module}):
        adapter = manager.get_adapter("openai", "gpt-4")

    assert adapter is fake_adapter
    fake_module.OpenAIAdapter.assert_called_once_with(
        api_key="sk-test", model="gpt-4"
    )


@patch("aria.llm.manager.settings")
def test_get_adapter_openrouter(mock_settings, manager):
    mock_settings.openrouter_api_key = "sk-or-test"

    fake_adapter = MagicMock()
    fake_module = MagicMock()
    fake_module.OpenRouterAdapter.return_value = fake_adapter

    with patch.dict("sys.modules", {"aria.llm.openrouter": fake_module}):
        adapter = manager.get_adapter("openrouter", "meta/llama-3")

    assert adapter is fake_adapter
    fake_module.OpenRouterAdapter.assert_called_once_with(
        api_key="sk-or-test", model="meta/llama-3"
    )


# ---------------------------------------------------------------------------
# is_backend_available tests
# ---------------------------------------------------------------------------


def test_is_backend_available_llamacpp(manager):
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        available, reason = manager.is_backend_available("llamacpp")
    assert available is True
    assert "available" in reason.lower()


@patch("aria.llm.manager.settings")
def test_is_backend_available_anthropic_no_key(mock_settings, manager):
    mock_settings.anthropic_api_key = ""
    available, reason = manager.is_backend_available("anthropic")
    assert available is False
    assert "not configured" in reason.lower()


@patch("aria.llm.manager.settings")
def test_is_backend_available_openai_no_key(mock_settings, manager):
    mock_settings.openai_api_key = ""
    available, reason = manager.is_backend_available("openai")
    assert available is False
    assert "not configured" in reason.lower()


@patch("aria.llm.manager.settings")
def test_is_backend_available_openrouter_no_key(mock_settings, manager):
    mock_settings.openrouter_api_key = ""
    available, reason = manager.is_backend_available("openrouter")
    assert available is False
    assert "not configured" in reason.lower()


def test_is_backend_available_unknown(manager):
    available, reason = manager.is_backend_available("deepseek")
    assert available is False
    assert "unknown" in reason.lower()


# ---------------------------------------------------------------------------
# close_all tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_all(manager):
    mock_adapter = MagicMock(spec=[])  # no attrs by default
    mock_adapter.client = MagicMock()
    mock_adapter.client.close = AsyncMock()
    manager.adapters["llamacpp:model"] = mock_adapter

    await manager.close_all()
    assert len(manager.adapters) == 0
    mock_adapter.client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_all_with_aexit(manager):
    mock_adapter = MagicMock()
    mock_adapter.__aexit__ = AsyncMock()
    manager.adapters["anthropic:claude"] = mock_adapter

    await manager.close_all()
    assert len(manager.adapters) == 0
    mock_adapter.__aexit__.assert_awaited_once_with(None, None, None)


@pytest.mark.asyncio
async def test_close_all_handles_errors(manager):
    """close_all should not raise even if an adapter errors on close."""
    mock_adapter = MagicMock(spec=[])
    mock_adapter.client = MagicMock()
    mock_adapter.client.close = AsyncMock(side_effect=RuntimeError("boom"))
    manager.adapters["openai:gpt"] = mock_adapter

    # Should not raise
    await manager.close_all()
    assert len(manager.adapters) == 0
