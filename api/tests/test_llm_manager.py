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


def test_llamacpp_adapter_labels_aria_owned_calls_as_background():
    from aria.llm import llamacpp

    with (
        patch.object(llamacpp, "OPENAI_AVAILABLE", True),
        patch.object(llamacpp, "AsyncOpenAI", create=True) as client,
    ):
        llamacpp.LlamaCppAdapter(
            base_url="http://localhost:8200/llm/v1",
            model="aria-resident",
            api_key="test",
        )

    assert client.call_args.kwargs["default_headers"] == {
        "X-Aria-Caller": "aria-background"
    }


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
    """No explicit base_url routes through the IDENTIFIED proxy.

    "The local model, whatever it is" is exactly the case where the agent cannot
    name its own model, so it goes via the proxy that injects the identity line.
    """
    mock_settings.llamacpp_url = "http://localhost:8080/v1"
    mock_settings.llamacpp_identified_url = "http://localhost:8080/v1-identified"
    mock_settings.llamacpp_api_key = ""

    fake_adapter = MagicMock()
    fake_module = MagicMock()
    fake_module.LlamaCppAdapter.return_value = fake_adapter

    with patch.dict("sys.modules", {"aria.llm.llamacpp": fake_module}):
        adapter = manager.get_adapter("llamacpp", "local-model")

    assert adapter is fake_adapter
    fake_module.LlamaCppAdapter.assert_called_once_with(
        base_url="http://localhost:8080/v1-identified",
        model="local-model",
        api_key="",
    )


@patch("aria.llm.manager.settings")
def test_get_adapter_llamacpp_explicit_base_url_is_untouched(mock_settings, manager):
    """An agent bound to a specific server goes direct, not via either proxy."""
    mock_settings.llamacpp_url = "http://localhost:8080/v1"
    mock_settings.llamacpp_identified_url = "http://localhost:8080/v1-identified"
    mock_settings.llamacpp_api_key = ""

    fake_adapter = MagicMock()
    fake_module = MagicMock()
    fake_module.LlamaCppAdapter.return_value = fake_adapter

    with patch.dict("sys.modules", {"aria.llm.llamacpp": fake_module}):
        adapter = manager.get_adapter(
            "llamacpp", "local-model", base_url="http://localhost:8108/v1"
        )

    assert adapter is fake_adapter
    fake_module.LlamaCppAdapter.assert_called_once_with(
        base_url="http://localhost:8108/v1",
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


@patch("aria.llm.manager.settings")
def test_get_adapter_agentic(mock_settings, manager):
    mock_settings.agentic_url = "http://localhost:8102/v1"
    mock_settings.agentic_api_key = ""

    fake_adapter = MagicMock()
    fake_module = MagicMock()
    fake_module.LlamaCppAdapter.return_value = fake_adapter

    with patch.dict("sys.modules", {"aria.llm.llamacpp": fake_module}):
        adapter = manager.get_adapter("agentic", "laguna-s-2.1")

    assert adapter is fake_adapter
    fake_module.LlamaCppAdapter.assert_called_once_with(
        base_url="http://localhost:8102/v1",
        model="laguna-s-2.1",
        api_key="",
    )


@patch("aria.llm.manager.settings")
def test_get_adapter_ridge(mock_settings, manager):
    """ridge reuses the llama.cpp adapter but with its own (much longer)
    timeout, since a cold Wake-on-LAN start is ~90s and must not be mistaken
    for a hung connection."""
    mock_settings.ridge_url = "http://100.123.245.84:8092/v1"
    mock_settings.ridge_api_key = ""
    mock_settings.ridge_timeout_seconds = 420

    fake_adapter = MagicMock()
    fake_module = MagicMock()
    fake_module.LlamaCppAdapter.return_value = fake_adapter

    with patch.dict("sys.modules", {"aria.llm.llamacpp": fake_module}):
        adapter = manager.get_adapter("ridge", "qwen3.6-35b-a3b")

    assert adapter is fake_adapter
    fake_module.LlamaCppAdapter.assert_called_once_with(
        base_url="http://100.123.245.84:8092/v1",
        model="qwen3.6-35b-a3b",
        api_key="",
        timeout_seconds=420,
    )


# ---------------------------------------------------------------------------
# is_backend_available tests
# ---------------------------------------------------------------------------


def test_is_backend_available_llamacpp(manager):
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        available, reason = manager.is_backend_available("llamacpp")
    assert available is True
    assert "available" in reason.lower()


def test_is_backend_available_agentic(manager):
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        available, reason = manager.is_backend_available("agentic")
    assert available is True


def test_is_backend_available_ridge_not_probed(manager):
    """ridge is reported available purely on config (openai SDK present) —
    it is deliberately NOT network-probed, since it sleeps by design and a
    probe would either wake it every tick or misreport it as down."""
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        available, reason = manager.is_backend_available("ridge")
    assert available is True
    assert "wake" in reason.lower()


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
