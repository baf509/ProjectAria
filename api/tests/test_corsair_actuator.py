from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from aria.infrastructure.corsair_actuator import (
    ActuatorRequest,
    ActuatorRequestError,
    execute,
    parse_request,
)
from aria.infrastructure.model_servers import ModelServerManager
from aria.infrastructure.model_servers import ModelServerNotFound


def _onbox_slug(manager: ModelServerManager) -> str:
    return next(spec.slug for spec in manager.specs() if spec.onbox)


def _offbox_slug(manager: ModelServerManager) -> str:
    return next(spec.slug for spec in manager.specs() if not spec.onbox)


def test_parse_accepts_only_exact_registry_capabilities():
    manager = ModelServerManager()
    slug = _onbox_slug(manager)

    assert parse_request(f"aria-model-actuator status {slug}", manager) == \
        ActuatorRequest("status", slug)
    assert parse_request(f"start {slug} --force", manager) == \
        ActuatorRequest("start", slug, force=True)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "shell",
        "exec /bin/sh",
        "start known --force extra",
        "stop known --force",
        "status known --debug",
        "start known; id",
    ],
)
def test_parse_rejects_shell_and_option_expansion(raw: str):
    with pytest.raises(ActuatorRequestError):
        parse_request(raw, ModelServerManager())


def test_parse_rejects_offbox_and_unknown_slugs():
    manager = ModelServerManager()
    with pytest.raises(ActuatorRequestError, match="not hosted on Corsair"):
        parse_request(f"start {_offbox_slug(manager)}", manager)
    with pytest.raises(ModelServerNotFound, match="Unknown model server"):
        parse_request("start definitely-not-a-model", manager)


@pytest.mark.asyncio
async def test_execute_calls_only_manager_lifecycle_methods():
    manager = AsyncMock(spec=ModelServerManager)
    manager.one.return_value = {"state": "running"}
    manager.start.return_value = {"action": "noop"}
    manager.stop.return_value = {"action": "stopped"}

    assert await execute(ActuatorRequest("status", "slug"), manager) == {"state": "running"}
    assert await execute(ActuatorRequest("start", "slug", True), manager) == {"action": "noop"}
    assert await execute(ActuatorRequest("stop", "slug"), manager) == {"action": "stopped"}
    manager.one.assert_awaited_once_with("slug")
    manager.start.assert_awaited_once_with("slug", force=True)
    manager.stop.assert_awaited_once_with("slug")
