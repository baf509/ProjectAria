"""Tests for the nudge-paused-shells endpoint (three-strikes + escalation)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from aria.config import settings


class _FakeShellColl:
    def __init__(self):
        self.doc = {"name": "claude-x"}
        self.updates = []

    async def find_one(self, q):
        return dict(self.doc)

    async def update_one(self, q, update, **kw):
        self.updates.append(update)
        self.doc.update(update.get("$set", {}))
        return MagicMock(matched_count=1)


class _FakeDB:
    def __init__(self):
        self.shells = _FakeShellColl()


class _FakeShellService:
    def __init__(self):
        self.shell = MagicMock()
        self.shell.tags = []
        self.row = {
            "name": "claude-x",
            "activity_state": "blocked",
            "awaiting_input": True,
            "idle_seconds": 900,
            "prompt_line": "> ",
            "last_line": "> ",
            "project_dir": "/tmp/demo",
        }
        self.send_input = AsyncMock(return_value=(7, "screen after"))

    async def get_shell(self, name):
        return self.shell if name == "claude-x" else None

    async def fleet_overview(self, **kw):
        return [self.row] if self.row else []


@pytest.fixture
async def nudge_client():
    from aria.main import app
    from aria.api import deps

    fake_db = _FakeDB()
    fake_shells = _FakeShellService()
    notifier = MagicMock()
    notifier.notify = AsyncMock(return_value={"queued": True})
    app.dependency_overrides[deps.get_db] = lambda: fake_db
    app.dependency_overrides[deps.get_shell_service] = lambda: fake_shells
    app.dependency_overrides[deps.get_notification_service] = lambda: notifier

    rl = MagicMock()
    rl.check = MagicMock(return_value=(True, 100))
    ks = MagicMock()
    ks.is_active = False
    estop = MagicMock()
    estop.is_active = AsyncMock(return_value=False)
    with (
        patch("aria.main.settings") as mock_settings,
        patch("aria.main.get_rate_limiter", return_value=rl),
        patch("aria.api.deps.get_killswitch", return_value=ks),
        patch("aria.api.deps.resolve_estop_manager", AsyncMock(return_value=estop)),
    ):
        mock_settings.api_auth_enabled = False
        mock_settings.cors_origins = ["*"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            ac.db = fake_db  # type: ignore[attr-defined]
            ac.shells = fake_shells  # type: ignore[attr-defined]
            ac.notifier = notifier  # type: ignore[attr-defined]
            ac.ks = ks  # type: ignore[attr-defined]
            yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_nudge_unknown_shell_404(nudge_client):
    resp = await nudge_client.post("/api/v1/shells/claude-nope/nudge", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_nudge_paused_shell_sends_default_text(nudge_client):
    resp = await nudge_client.post("/api/v1/shells/claude-x/nudge", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["nudged"] is True
    assert body["attempts"] == 1
    assert body["escalated"] is False
    assert body["screen"] == "screen after"
    sent_text = nudge_client.shells.send_input.await_args.args[1]
    assert sent_text == settings.shells_nudge_default_text
    assert nudge_client.db.shells.doc["nudge_attempts"] == 1


@pytest.mark.asyncio
async def test_nudge_safe_prompt_sends_bare_enter(nudge_client):
    nudge_client.shells.row["prompt_line"] = "Press Enter to continue"
    resp = await nudge_client.post("/api/v1/shells/claude-x/nudge", json={})
    assert resp.json()["nudged"] is True
    assert nudge_client.shells.send_input.await_args.args[1] == ""


@pytest.mark.asyncio
async def test_nudge_not_paused_resets_attempts(nudge_client):
    nudge_client.db.shells.doc["nudge_attempts"] = 2
    nudge_client.shells.row["activity_state"] = "working"
    nudge_client.shells.row["awaiting_input"] = False
    resp = await nudge_client.post("/api/v1/shells/claude-x/nudge", json={})
    body = resp.json()
    assert body == {"nudged": False, "reason": "not_paused", "attempts": 0}
    assert nudge_client.db.shells.doc["nudge_attempts"] == 0


@pytest.mark.asyncio
async def test_nudge_third_attempt_escalates_and_resets(nudge_client):
    nudge_client.db.shells.doc["nudge_attempts"] = 2
    resp = await nudge_client.post("/api/v1/shells/claude-x/nudge", json={})
    body = resp.json()
    assert body["nudged"] is True
    assert body["attempts"] == 3
    assert body["escalated"] is True
    nudge_client.notifier.notify.assert_awaited_once()
    kwargs = nudge_client.notifier.notify.await_args.kwargs
    assert kwargs["source"] == "shells:nudge"
    assert kwargs["project_path"] == "/tmp/demo"
    assert nudge_client.db.shells.doc["nudge_attempts"] == 0


@pytest.mark.asyncio
async def test_nudge_debounced_by_last_nudge_at(nudge_client):
    nudge_client.db.shells.doc["nudge_last_at"] = datetime.now(timezone.utc) - timedelta(
        minutes=1
    )
    nudge_client.db.shells.doc["nudge_attempts"] = 1
    resp = await nudge_client.post("/api/v1/shells/claude-x/nudge", json={})
    assert resp.json() == {"nudged": False, "reason": "recently_nudged", "attempts": 1}


@pytest.mark.asyncio
async def test_nudge_freshly_paused_skipped_unless_forced(nudge_client):
    nudge_client.shells.row["idle_seconds"] = 30
    resp = await nudge_client.post("/api/v1/shells/claude-x/nudge", json={})
    assert resp.json()["reason"] == "paused_too_recently"

    resp = await nudge_client.post("/api/v1/shells/claude-x/nudge", json={"force": True})
    assert resp.json()["nudged"] is True


@pytest.mark.asyncio
async def test_nudge_protected_tag(nudge_client):
    nudge_client.shells.shell.tags = ["no-nudge"]
    resp = await nudge_client.post("/api/v1/shells/claude-x/nudge", json={})
    assert resp.json() == {"nudged": False, "reason": "protected", "attempts": 0}


@pytest.mark.asyncio
async def test_nudge_blocked_by_killswitch(nudge_client):
    nudge_client.ks.is_active = True
    resp = await nudge_client.post("/api/v1/shells/claude-x/nudge", json={})
    assert resp.status_code == 409
