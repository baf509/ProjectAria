"""Route tests for the watched shells subsystem."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from aria.shells.models import Shell, ShellEvent
from aria.shells.service import (
    ShellAlreadyExistsError,
    ShellNotFoundError,
    ShellStoppedError,
)


class FakeShellService:
    def __init__(self):
        self.shells: dict[str, Shell] = {}
        self.events_by_shell: dict[str, list[ShellEvent]] = {}
        self.sent: list[tuple[str, str, bool, bool]] = []
        self.tags: dict[str, list[str]] = {}
        self.created: list[tuple[str, str, bool]] = []
        self.killed: list[str] = []
        self.purged: list[str] = []
        self.raise_on_send: Exception | None = None
        self.events = MagicMock()  # unused in these tests

    async def list_shells(self, status=None):
        out = list(self.shells.values())
        if status:
            out = [s for s in out if s.status in status]
        return out

    async def get_shell(self, name):
        return self.shells.get(name)

    async def list_events(self, name, **kwargs):
        return list(self.events_by_shell.get(name, []))

    async def get_last_snapshot(self, name):
        return None

    async def send_input(self, name, text, *, append_enter=True, literal=False):
        if self.raise_on_send:
            raise self.raise_on_send
        if name not in self.shells:
            raise ShellNotFoundError(name)
        if self.shells[name].status == "stopped":
            raise ShellStoppedError(name)
        self.sent.append((name, text, append_enter, literal))
        return 42

    async def set_tags(self, name, tags):
        self.tags[name] = list(tags)
        if name in self.shells:
            self.shells[name].tags = list(tags)

    async def create_shell(self, name, *, workdir="", launch_claude=True, cols=None, rows=None):
        full_name = name if name.startswith("claude-") else f"claude-{name}"
        existing = self.shells.get(full_name)
        if existing is not None:
            if existing.status in ("active", "idle"):
                raise ShellAlreadyExistsError(full_name)
            existing.status = "active"
            existing.project_dir = workdir or existing.project_dir
            self.created.append((full_name, workdir, launch_claude))
            return existing
        shell = _make_shell(name=full_name)
        shell.project_dir = workdir
        self.shells[full_name] = shell
        self.created.append((full_name, workdir, launch_claude))
        self.last_geometry = (cols, rows)
        return shell

    async def resize_shell(self, name, cols, rows):
        if name not in self.shells:
            from aria.shells.tmux import TmuxSessionNotFoundError
            raise TmuxSessionNotFoundError(name)
        self.resized = (name, cols, rows)

    async def kill_shell(self, name):
        self.killed.append(name)
        if name in self.shells:
            self.shells[name].status = "stopped"

    async def purge_shell(self, name):
        self.purged.append(name)
        self.shells.pop(name, None)
        return {"shells": 1, "events": 0, "snapshots": 0}


def _make_shell(name="claude-proj", status="active") -> Shell:
    now = datetime.now(timezone.utc)
    return Shell(
        name=name,
        short_name=name.replace("claude-", ""),
        project_dir="/tmp/proj",
        host="test",
        status=status,
        created_at=now,
        last_activity_at=now,
        line_count=10,
        tags=[],
    )


def _make_event(name="claude-proj", line=1, text="hello") -> ShellEvent:
    return ShellEvent(
        shell_name=name,
        ts=datetime.now(timezone.utc),
        line_number=line,
        kind="output",
        text_raw=text,
        text_clean=text,
        source="pipe-pane",
    )


@pytest.fixture
async def client():
    from aria.main import app
    from aria.api import deps

    fake = FakeShellService()
    app.dependency_overrides[deps.get_shell_service] = lambda: fake
    app.dependency_overrides[deps.get_db] = lambda: MagicMock()

    rl = MagicMock()
    rl.check = MagicMock(return_value=(True, 100))
    with (
        patch("aria.main.settings") as mock_settings,
        patch("aria.main.get_rate_limiter", return_value=rl),
    ):
        mock_settings.api_auth_enabled = False
        mock_settings.cors_origins = ["*"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            ac.fake_service = fake  # type: ignore[attr-defined]
            yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_shells_empty(client):
    resp = await client.get("/api/v1/shells")
    assert resp.status_code == 200
    assert resp.json() == {"shells": []}


@pytest.mark.asyncio
async def test_list_shells_returns_shells(client):
    client.fake_service.shells["claude-proj"] = _make_shell()
    resp = await client.get("/api/v1/shells")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["shells"]) == 1
    assert data["shells"][0]["name"] == "claude-proj"


@pytest.mark.asyncio
async def test_get_shell_not_found(client):
    resp = await client.get("/api/v1/shells/claude-missing")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_shell_ok(client):
    client.fake_service.shells["claude-proj"] = _make_shell()
    resp = await client.get("/api/v1/shells/claude-proj")
    assert resp.status_code == 200
    assert resp.json()["short_name"] == "proj"


@pytest.mark.asyncio
async def test_list_events(client):
    client.fake_service.shells["claude-proj"] = _make_shell()
    client.fake_service.events_by_shell["claude-proj"] = [_make_event(line=1), _make_event(line=2, text="bye")]
    resp = await client.get("/api/v1/shells/claude-proj/events?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 2
    assert data["events"][1]["text_clean"] == "bye"


@pytest.mark.asyncio
async def test_send_input_ok(client):
    client.fake_service.shells["claude-proj"] = _make_shell()
    resp = await client.post(
        "/api/v1/shells/claude-proj/input",
        json={"text": "yes please", "append_enter": True, "literal": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["line_number"] == 42
    assert client.fake_service.sent == [("claude-proj", "yes please", True, False)]


@pytest.mark.asyncio
async def test_send_input_missing(client):
    resp = await client.post(
        "/api/v1/shells/claude-missing/input",
        json={"text": "hi"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_send_input_stopped(client):
    client.fake_service.shells["claude-proj"] = _make_shell(status="stopped")
    resp = await client.post(
        "/api/v1/shells/claude-proj/input",
        json={"text": "hi"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_set_tags(client):
    client.fake_service.shells["claude-proj"] = _make_shell()
    resp = await client.post(
        "/api/v1/shells/claude-proj/tags",
        json={"tags": ["primary", "urgent"]},
    )
    assert resp.status_code == 200
    assert client.fake_service.tags["claude-proj"] == ["primary", "urgent"]


@pytest.mark.asyncio
async def test_create_shell_ok(client):
    resp = await client.post(
        "/api/v1/shells",
        json={"name": "newproj", "workdir": "/tmp/newproj"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "claude-newproj"
    assert data["project_dir"] == "/tmp/newproj"
    assert client.fake_service.created == [("claude-newproj", "/tmp/newproj", True)]


@pytest.mark.asyncio
async def test_create_shell_keeps_existing_prefix(client):
    resp = await client.post(
        "/api/v1/shells",
        json={"name": "claude-already", "launch_claude": False},
    )
    assert resp.status_code == 201
    assert client.fake_service.created == [("claude-already", "", False)]


@pytest.mark.asyncio
async def test_create_shell_conflict(client):
    client.fake_service.shells["claude-proj"] = _make_shell()
    resp = await client.post(
        "/api/v1/shells",
        json={"name": "proj"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_shell_reclaims_stopped(client):
    stopped = _make_shell(status="stopped")
    client.fake_service.shells["claude-proj"] = stopped
    resp = await client.post(
        "/api/v1/shells",
        json={"name": "proj", "workdir": "/tmp/proj2"},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "active"
    assert client.fake_service.shells["claude-proj"].status == "active"


@pytest.mark.asyncio
async def test_create_shell_rejects_bad_name(client):
    resp = await client.post(
        "/api/v1/shells",
        json={"name": "has spaces"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_shell_ok(client):
    client.fake_service.shells["claude-proj"] = _make_shell()
    resp = await client.delete("/api/v1/shells/claude-proj")
    assert resp.status_code == 204
    assert client.fake_service.killed == ["claude-proj"]
    assert client.fake_service.shells["claude-proj"].status == "stopped"


@pytest.mark.asyncio
async def test_delete_shell_purge(client):
    client.fake_service.shells["claude-proj"] = _make_shell()
    resp = await client.delete("/api/v1/shells/claude-proj?purge=true")
    assert resp.status_code == 204
    assert client.fake_service.purged == ["claude-proj"]
    assert client.fake_service.killed == []
    assert "claude-proj" not in client.fake_service.shells


@pytest.mark.asyncio
async def test_delete_shell_missing(client):
    resp = await client.delete("/api/v1/shells/claude-missing")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_shell_passes_geometry(client):
    resp = await client.post(
        "/api/v1/shells",
        json={"name": "proj", "cols": 100, "rows": 30},
    )
    assert resp.status_code == 201
    assert client.fake_service.last_geometry == (100, 30)


@pytest.mark.asyncio
async def test_resize_shell_ok(client):
    client.fake_service.shells["claude-proj"] = _make_shell()
    resp = await client.post(
        "/api/v1/shells/claude-proj/resize",
        json={"cols": 132, "rows": 50},
    )
    assert resp.status_code == 204
    assert client.fake_service.resized == ("claude-proj", 132, 50)


@pytest.mark.asyncio
async def test_resize_shell_missing(client):
    resp = await client.post(
        "/api/v1/shells/claude-missing/resize",
        json={"cols": 132, "rows": 50},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resize_shell_rejects_bad_geometry(client):
    client.fake_service.shells["claude-proj"] = _make_shell()
    resp = await client.post(
        "/api/v1/shells/claude-proj/resize",
        json={"cols": 5, "rows": 50},  # below min cols
    )
    assert resp.status_code == 422
