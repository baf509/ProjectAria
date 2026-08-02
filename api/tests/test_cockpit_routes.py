"""Route + helper tests for the C4 Project Switcher / Cockpit read models."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from aria.api.routes.digest import attention_score, path_in_project, project_roots
from tests.test_planning_routes import FakePlanningService, _make_project, _make_task


# ----------------------------------------------------------------- helpers

def test_path_in_project_prefix_semantics():
    roots = ["/home/ben/Development/ProjectAria"]
    assert path_in_project("/home/ben/Development/ProjectAria", roots)
    assert path_in_project("/home/ben/Development/ProjectAria/api", roots)
    assert not path_in_project("/home/ben/Development/ProjectAria2", roots)
    assert not path_in_project(None, roots)
    assert not path_in_project("/elsewhere", roots)


def test_project_roots_dedup_and_normalize():
    p = _make_project().model_copy(
        update={"path": "/tmp/demo/", "relevant_paths": ["/tmp/demo", "/tmp/other"]}
    )
    assert project_roots(p) == ["/tmp/demo", "/tmp/other"]


def test_path_index_most_specific_project_wins():
    """Regression: a coarse parent project (e.g. a row for ~/Development
    itself) must NOT swallow activity that belongs to a child project."""
    from aria.api.routes.digest import PathIndex

    parent = _make_project("PP", slug="development").model_copy(
        update={"path": "/home/ben/Development", "relevant_paths": []}
    )
    child = _make_project("PC", slug="aria").model_copy(
        update={"path": "/home/ben/Development/ProjectAria", "relevant_paths": []}
    )
    idx = PathIndex([parent, child])
    assert idx.owner("/home/ben/Development/ProjectAria/api") == "PC"
    assert idx.owner("/home/ben/Development/ProjectAria") == "PC"
    assert idx.owner("/home/ben/Development/scratch.txt".rsplit("/", 1)[0]) == "PP"
    assert idx.owner("/home/ben/Development/other-repo") == "PP"
    assert idx.owner("/elsewhere") is None
    # Session fallback: workspace outside any root -> source_repo attributes.
    assert (
        idx.session_owner(
            {"workspace": "/tmp/worktree-x", "source_repo": "/home/ben/Development/ProjectAria"}
        )
        == "PC"
    )


def test_attention_score_weights_blocked_highest():
    blocked = attention_score({"blocked_shells": 1})
    gate = attention_score({"gate_failed_sessions": 1})
    alert = attention_score({"unacked_alerts": 1})
    stale = attention_score({"stale_tasks": 1})
    assert blocked > gate > alert > stale > 0
    assert attention_score({"stale_tasks": 50}) == 5  # capped


# ----------------------------------------------------------------- fakes

class _FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *a, **k):
        return self

    async def to_list(self, length=None):
        return self.docs[: length if length else None]


class _FakeColl:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.one = None
        self.updates = []

    def find(self, *a, **k):
        return _FakeCursor(self.docs)

    async def find_one(self, *a, **k):
        return self.one

    async def update_one(self, filt, update, upsert=False):
        self.updates.append((filt, update, upsert))
        self.one = {**(self.one or {}), **update.get("$set", {})}
        return MagicMock(matched_count=1)


class _FakeDB:
    def __init__(self):
        self.coding_sessions = _FakeColl()
        self.alerts = _FakeColl()
        self.app_state = _FakeColl()
        self.memories = _FakeColl()
        self.tasks = _FakeColl()
        self.usage = _FakeColl()


class _FakeShellService:
    def __init__(self, shells=None):
        self._shells = shells or []

    async def fleet_overview(self, **kw):
        return self._shells


@pytest.fixture
async def cockpit_client():
    from aria.main import app
    from aria.api import deps

    fake_planning = FakePlanningService()
    fake_db = _FakeDB()
    fake_shells = _FakeShellService()
    app.dependency_overrides[deps.get_planning_service] = lambda: fake_planning
    app.dependency_overrides[deps.get_db] = lambda: fake_db
    app.dependency_overrides[deps.get_shell_service] = lambda: fake_shells

    rl = MagicMock()
    rl.check = MagicMock(return_value=(True, 100))
    with (
        patch("aria.main.settings") as mock_settings,
        patch("aria.main.get_rate_limiter", return_value=rl),
    ):
        mock_settings.api_auth_enabled = False
        mock_settings.cors_origins = ["*"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            ac.planning = fake_planning  # type: ignore[attr-defined]
            ac.db = fake_db  # type: ignore[attr-defined]
            ac.shells = fake_shells  # type: ignore[attr-defined]
            yield ac
    app.dependency_overrides.clear()


def _seed_project(client, slug="demo", path="/tmp/demo", pid="P1"):
    p = _make_project(pid, slug=slug).model_copy(update={"path": path})
    client.planning.projects[pid] = p
    return p


# ----------------------------------------------------------------- overview

@pytest.mark.asyncio
async def test_overview_empty(cockpit_client):
    resp = await cockpit_client.get("/api/v1/projects/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["projects"] == []
    assert body["active_project"] is None


@pytest.mark.asyncio
async def test_overview_ranks_blocked_project_first(cockpit_client):
    _seed_project(cockpit_client, slug="calm", path="/tmp/calm", pid="P1")
    _seed_project(cockpit_client, slug="loud", path="/tmp/loud", pid="P2")
    cockpit_client.shells._shells = [
        {"name": "claude-a", "project_dir": "/tmp/loud", "activity_state": "blocked",
         "awaiting_input": True},
        {"name": "claude-b", "project_dir": "/tmp/calm", "activity_state": "working",
         "awaiting_input": False},
    ]
    resp = await cockpit_client.get("/api/v1/projects/overview")
    assert resp.status_code == 200
    rows = resp.json()["projects"]
    assert [r["slug"] for r in rows] == ["loud", "calm"]
    assert rows[0]["attention"]["blocked_shells"] == 1
    assert rows[0]["attention_score"] > rows[1]["attention_score"]


@pytest.mark.asyncio
async def test_overview_counts_gate_failures_and_sessions(cockpit_client):
    _seed_project(cockpit_client, slug="demo", path="/tmp/demo", pid="P1")
    cockpit_client.db.coding_sessions.docs = [
        {
            "_id": "s1",
            "status": "running",
            "workspace": "/tmp/demo",
            "gate_runs": [{"at": datetime.now(timezone.utc), "passed": False, "tail": "x"}],
        }
    ]
    resp = await cockpit_client.get("/api/v1/projects/overview")
    att = resp.json()["projects"][0]["attention"]
    assert att["running_sessions"] == 1
    assert att["gate_failed_sessions"] == 1


@pytest.mark.asyncio
async def test_overview_parent_project_does_not_swallow_child(cockpit_client):
    _seed_project(cockpit_client, slug="development", path="/tmp/dev", pid="PP")
    _seed_project(cockpit_client, slug="child", path="/tmp/dev/child", pid="PC")
    cockpit_client.shells._shells = [
        {"name": "claude-c", "project_dir": "/tmp/dev/child", "activity_state": "blocked",
         "awaiting_input": True},
    ]
    resp = await cockpit_client.get("/api/v1/projects/overview")
    rows = {r["slug"]: r for r in resp.json()["projects"]}
    assert rows["child"]["attention"]["blocked_shells"] == 1
    assert rows["development"]["attention"]["blocked_shells"] == 0
    assert rows["development"]["attention_score"] == 0


# ------------------------------------------------------------- active focus

@pytest.mark.asyncio
async def test_set_and_get_active_project(cockpit_client):
    _seed_project(cockpit_client, slug="demo")
    resp = await cockpit_client.put("/api/v1/projects/active", json={"slug": "demo"})
    assert resp.status_code == 200
    assert resp.json()["active_project"] == "demo"

    resp = await cockpit_client.get("/api/v1/projects/active")
    assert resp.json()["active_project"] == "demo"

    resp = await cockpit_client.put("/api/v1/projects/active", json={"slug": None})
    assert resp.json()["active_project"] is None


@pytest.mark.asyncio
async def test_set_active_project_unknown_slug_404(cockpit_client):
    resp = await cockpit_client.put("/api/v1/projects/active", json={"slug": "nope"})
    assert resp.status_code == 404


# ----------------------------------------------------------------- cockpit

@pytest.mark.asyncio
async def test_cockpit_unknown_project_404(cockpit_client):
    resp = await cockpit_client.get("/api/v1/projects/nope/cockpit")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cockpit_aggregates_scoped_data(cockpit_client):
    p = _seed_project(cockpit_client, slug="demo", path="/tmp/demo", pid="P1")
    now = datetime.now(timezone.utc)
    cockpit_client.shells._shells = [
        {"name": "claude-x", "project_dir": "/tmp/demo", "activity_state": "blocked",
         "awaiting_input": True},
        {"name": "claude-y", "project_dir": "/tmp/other", "activity_state": "working",
         "awaiting_input": False},
    ]
    cockpit_client.db.coding_sessions.docs = [
        {"_id": "s1", "status": "completed", "workspace": "/tmp/demo",
         "gate_runs": [{"at": now, "passed": True, "tail": "ok"}],
         "updated_at": now, "created_at": now},
        {"_id": "s2", "status": "running", "workspace": "/tmp/other",
         "updated_at": now, "created_at": now},
    ]
    cockpit_client.db.alerts.docs = [
        {"_id": "a1", "source": "coding:gate", "event_type": "gate:failed",
         "message": "m", "project_path": "/tmp/demo", "created_at": now},
        {"_id": "a2", "source": "selfcheck", "event_type": "x",
         "message": "m2", "created_at": now},
    ]
    cockpit_client.db.memories.docs = [
        {"content": "repo changed", "created_at": now,
         "source": {"type": "machine_scan", "repo": "/tmp/demo"}},
    ]
    stale = _make_task("t1", project_id="P1").model_copy(
        update={"updated_at": now - timedelta(days=30)}
    )
    cockpit_client.planning.tasks["t1"] = stale

    resp = await cockpit_client.get("/api/v1/projects/demo/cockpit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"]["slug"] == "demo"
    assert [s["name"] for s in body["shells"]] == ["claude-x"]
    assert [s["id"] for s in body["sessions"]] == ["s1"]
    assert len(body["alerts"]) == 1 and body["alerts"][0]["id"] == "a1"
    assert body["changed"][0]["content"] == "repo changed"
    assert body["tasks"][0]["stale"] is True
    assert body["attention"]["blocked_shells"] == 1
    assert body["budget"]["sessions_priced"] == 0
