"""Tests for aria.shells.context.build_shell_context."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aria.shells.context import build_shell_context
from aria.shells.models import Shell, ShellEvent


class FakeShellService:
    def __init__(self, shells: list[Shell], events: dict[str, list[ShellEvent]]):
        self._shells = shells
        self._events = events

    async def list_shells(self, status=None):
        out = list(self._shells)
        if status:
            out = [s for s in out if s.status in status]
        return out

    async def tail(self, name, *, lines=20):
        return list(self._events.get(name, []))[-lines:]


def _shell(name="claude-proj", status="active", age_minutes=5) -> Shell:
    now = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return Shell(
        name=name,
        short_name=name.replace("claude-", ""),
        project_dir="/tmp/p",
        host="h",
        status=status,
        created_at=now,
        last_activity_at=now,
        line_count=5,
        tags=[],
    )


def _event(name="claude-proj", line=1, kind="output", text="hello") -> ShellEvent:
    return ShellEvent(
        shell_name=name,
        ts=datetime.now(timezone.utc),
        line_number=line,
        kind=kind,
        text_raw=text,
        text_clean=text,
        source="pipe-pane",
    )


@pytest.mark.asyncio
async def test_empty_when_no_shells():
    svc = FakeShellService([], {})
    assert await build_shell_context(svc) == ""


@pytest.mark.asyncio
async def test_skips_stale_shells():
    svc = FakeShellService(
        [_shell(age_minutes=60 * 48)],  # older than 24h default
        {"claude-proj": [_event()]},
    )
    assert await build_shell_context(svc) == ""


@pytest.mark.asyncio
async def test_renders_section_with_input_marker():
    svc = FakeShellService(
        [_shell()],
        {
            "claude-proj": [
                _event(line=1, kind="output", text="hello world"),
                _event(line=2, kind="input", text="yes"),
            ]
        },
    )
    block = await build_shell_context(svc)
    assert "Watched Shells" in block
    assert "claude-proj".replace("claude-", "proj") in block or "proj" in block
    assert "hello world" in block
    assert "> yes" in block


@pytest.mark.asyncio
async def test_skips_shell_with_no_events():
    svc = FakeShellService([_shell()], {"claude-proj": []})
    assert await build_shell_context(svc) == ""
