"""Tests for aria.shells.notifier.IdleNotifier._tick."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from aria.notifications.service import NotificationService
from aria.shells.models import Shell, ShellEvent
from aria.shells.notifier import IdleNotifier


class FakeShellService:
    def __init__(self, shells, events):
        self._shells = shells
        self._events = events

    async def list_shells(self, status=None):
        return [s for s in self._shells if not status or s.status in status]

    async def tail(self, name, *, lines=5):
        return list(self._events.get(name, []))[-lines:]


def _shell(name="claude-proj", idle_seconds=3600) -> Shell:
    now = datetime.now(timezone.utc) - timedelta(seconds=idle_seconds)
    return Shell(
        name=name, short_name="proj", project_dir="", host="h",
        status="active", created_at=now, last_activity_at=now,
        line_count=1, tags=[],
    )


def _event(line=1, kind="output", text="Continue? [y/n] ") -> ShellEvent:
    return ShellEvent(
        shell_name="claude-proj",
        ts=datetime.now(timezone.utc),
        line_number=line,
        kind=kind,
        text_raw=text,
        text_clean=text,
        source="pipe-pane",
    )


@pytest.mark.asyncio
async def test_notifies_on_idle_prompt():
    svc = FakeShellService([_shell()], {"claude-proj": [_event()]})
    notif = AsyncMock()
    notif.notify = AsyncMock(return_value={"sent": True})
    n = IdleNotifier(svc, notif)
    await n._tick()
    notif.notify.assert_called_once()
    args = notif.notify.call_args.kwargs
    assert args["source"] == "shells"
    assert args["event_type"] == "idle_prompt"


@pytest.mark.asyncio
async def test_deduplicates_same_line():
    svc = FakeShellService([_shell()], {"claude-proj": [_event()]})
    notif = AsyncMock()
    notif.notify = AsyncMock(return_value={"sent": True})
    n = IdleNotifier(svc, notif)
    await n._tick()
    await n._tick()
    assert notif.notify.call_count == 1


@pytest.mark.asyncio
async def test_skips_recent_activity():
    svc = FakeShellService([_shell(idle_seconds=5)], {"claude-proj": [_event()]})
    notif = AsyncMock()
    n = IdleNotifier(svc, notif)
    await n._tick()
    notif.notify.assert_not_called()


@pytest.mark.asyncio
async def test_skips_non_prompt_output():
    svc = FakeShellService(
        [_shell()],
        {"claude-proj": [_event(text="just some normal output")]},
    )
    notif = AsyncMock()
    n = IdleNotifier(svc, notif)
    await n._tick()
    notif.notify.assert_not_called()


@pytest.mark.asyncio
async def test_skips_input_event_last():
    svc = FakeShellService(
        [_shell()],
        {"claude-proj": [_event(kind="input", text="Continue? [y/n] ")]},
    )
    notif = AsyncMock()
    n = IdleNotifier(svc, notif)
    await n._tick()
    notif.notify.assert_not_called()


class TestAgentLifecycleIsNotAnAlert:
    """The watchdog re-publishes orchestrator mail as source="agents"
    (agents/watchdog.py _drain_orchestrator_mail). That bypassed the
    coding:*/task informational drop, so every finished sub-agent enqueued an
    alert -> Hermes triage spawned a fixer -> the fixer finished -> another
    alert. A closed feedback loop; 31 of these accumulated in prod.
    """

    @staticmethod
    def _svc():
        svc = NotificationService.__new__(NotificationService)
        svc._cooldowns = {}
        return svc

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event_type", ["agent_task_done", "agent_mail"])
    async def test_lifecycle_mail_is_dropped(self, event_type):
        res = await self._svc().notify(source="agents", event_type=event_type, detail="x")
        assert res == {"queued": False, "reason": "informational"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event_type", ["agent_error", "agent_handoff"])
    async def test_real_agent_problems_still_alert(self, event_type):
        # Must NOT be dropped as informational — these want triage. The enqueue
        # itself fails here (no DB in unit tests); the point is it got past the guard.
        with patch("aria.db.mongodb.get_database", new=AsyncMock(side_effect=RuntimeError("no db"))):
            res = await self._svc().notify(source="agents", event_type=event_type, detail="x")
        assert res.get("reason") != "informational"
