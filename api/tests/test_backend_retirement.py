"""Regression coverage for providers deliberately removed from ARIA."""

import pytest

from aria.config import settings
from aria.core.router import suggest_backend
from aria.llm.manager import LLMManager


def test_fireworks_is_not_a_configured_provider() -> None:
    assert not hasattr(settings, "fireworks_api_key")
    assert not hasattr(settings, "fireworks_base_url")


def test_fireworks_adapter_cannot_be_created() -> None:
    with pytest.raises(ValueError, match="Unknown backend: fireworks"):
        LLMManager().get_adapter("fireworks", "retired")


def test_router_uses_only_local_backends() -> None:
    assert suggest_backend("architect a complex system")[0] == "agentic"
    assert suggest_backend("ordinary request with no routing hint")[0] == "llamacpp"
