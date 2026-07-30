"""Tests for aria.shells.selfcheck — specifically that a deliberately-disabled
backend is not reported as an incident."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aria.shells import selfcheck


class _EmptyCursor:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeCollection:
    def find(self, *args, **kwargs):
        return _EmptyCursor()


class _FakeDB:
    def __init__(self):
        self.shell_extraction_state = _FakeCollection()

    async def command(self, *_a, **_kw):
        return {"ok": 1}

    def __getattr__(self, _name):
        return _FakeCollection()


async def _run(pool_enabled: bool) -> list[str]:
    with patch.object(selfcheck.settings, "pool_enabled", pool_enabled), \
         patch.object(selfcheck, "_check_http", AsyncMock(return_value=(True, "HTTP 200"))), \
         patch.object(selfcheck, "_check_gtt", lambda: (True, "ok")):
        checks = await selfcheck.run_checks(_FakeDB())
    return [c["name"] for c in checks]


@pytest.mark.asyncio
async def test_chadrock_not_probed_when_pool_disabled():
    """Ben shut chadrock down on purpose (pool_enabled=false). Probing it
    anyway reported DEGRADED every tick, and each alert woke the Hermes
    alert-triage cron into spawning a diagnostic agent for a non-incident."""
    assert "chadrock" not in await _run(pool_enabled=False)


@pytest.mark.asyncio
async def test_chadrock_probed_when_pool_enabled():
    """The monitoring must come back the moment the server is legitimately
    in service again — this check exists because a chadrock crash used to
    page nobody at all."""
    assert "chadrock" in await _run(pool_enabled=True)
