"""Route tests for the planning subsystem (todos + projects)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from aria.planning.models import (
    Project,
    ProjectActivity,
    ProjectCreateRequest,
    ProjectUpdateRequest,
    Task,
    TaskCreateRequest,
    TaskSource,
    TaskStatus,
    TaskUpdateRequest,
)


def _make_task(
    task_id: str = "111111111111111111111111",
    title: str = "Test task",
    status: TaskStatus = "active",
    project_id: str | None = None,
) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        title=title,
        notes=None,
        status=status,
        due_at=None,
        project_id=project_id,
        tags=[],
        source=TaskSource(type="manual"),
        content_hash="x" * 64,
        created_at=now,
        updated_at=now,
        completed_at=now if status == "done" else None,
    )


def _make_project(project_id: str = "222222222222222222222222", slug: str = "demo") -> Project:
    now = datetime.now(timezone.utc)
    return Project(
        id=project_id,
        name="Demo project",
        slug=slug,
        summary="A demo",
        status="active",
        next_steps=["ship v1"],
        relevant_paths=["/tmp/demo"],
        tags=[],
        recent_activity=[],
        created_at=now,
        updated_at=now,
        last_signal_at=None,
    )


class FakePlanningService:
    """In-memory stand-in for PlanningService used by route tests."""

    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.projects: dict[str, Project] = {}

    # ---- task ops
    async def list_tasks(self, *, status=None, project_id=None, limit=200):
        out = list(self.tasks.values())
        if status:
            out = [t for t in out if t.status in status]
        if project_id:
            out = [t for t in out if t.project_id == project_id]
        return out[:limit]

    async def create_task(self, body: TaskCreateRequest, *, source=None):
        task_id = f"{len(self.tasks):024d}"
        t = _make_task(task_id, title=body.title, status=body.status, project_id=body.project_id)
        self.tasks[task_id] = t
        return t

    async def get_task(self, task_id: str):
        return self.tasks.get(task_id)

    async def update_task(self, task_id: str, body: TaskUpdateRequest):
        existing = self.tasks.get(task_id)
        if not existing:
            return None
        update = body.model_dump(exclude_unset=True)
        merged = existing.model_dump()
        merged.update({k: v for k, v in update.items() if v is not None or k in update})
        merged["updated_at"] = datetime.now(timezone.utc)
        if update.get("status") == "done":
            merged["completed_at"] = datetime.now(timezone.utc)
        new = Task(**merged)
        self.tasks[task_id] = new
        return new

    async def set_task_status(self, task_id: str, status: TaskStatus):
        return await self.update_task(task_id, TaskUpdateRequest(status=status))

    async def delete_task(self, task_id: str):
        return self.tasks.pop(task_id, None) is not None

    # ---- project ops
    async def list_projects(self, *, status=None):
        out = list(self.projects.values())
        if status:
            out = [p for p in out if p.status == status]
        return out

    async def create_project(self, body: ProjectCreateRequest):
        project_id = f"P{len(self.projects):023d}"
        slug = body.slug or body.name.lower().replace(" ", "-")
        p = _make_project(project_id, slug=slug)
        # Apply request fields
        p_dict = p.model_dump()
        p_dict.update({"name": body.name, "summary": body.summary, "status": body.status})
        new = Project(**p_dict)
        self.projects[project_id] = new
        return new

    async def get_project(self, project_id: str):
        return self.projects.get(project_id)

    async def update_project(self, project_id: str, body: ProjectUpdateRequest):
        existing = self.projects.get(project_id)
        if not existing:
            return None
        merged = existing.model_dump()
        for k, v in body.model_dump(exclude_unset=True).items():
            merged[k] = v
        new = Project(**merged)
        self.projects[project_id] = new
        return new

    async def delete_project(self, project_id: str):
        if project_id not in self.projects:
            return False
        del self.projects[project_id]
        for t in self.tasks.values():
            if t.project_id == project_id:
                t.project_id = None  # detach
        return True


@pytest.fixture
async def client():
    from aria.main import app
    from aria.api import deps

    fake = FakePlanningService()
    app.dependency_overrides[deps.get_planning_service] = lambda: fake
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


# ----------------------------------------------------------------- todos

@pytest.mark.asyncio
async def test_list_todos_empty(client):
    resp = await client.get("/api/v1/todos")
    assert resp.status_code == 200
    assert resp.json() == {"tasks": []}


@pytest.mark.asyncio
async def test_create_todo(client):
    resp = await client.post("/api/v1/todos", json={"title": "Buy milk"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Buy milk"
    assert body["status"] == "active"


@pytest.mark.asyncio
async def test_create_todo_rejects_empty_title(client):
    resp = await client.post("/api/v1/todos", json={"title": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_todo_not_found(client):
    resp = await client.get("/api/v1/todos/missing")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_accept_todo(client):
    client.fake_service.tasks["t1"] = _make_task(task_id="t1", status="proposed")
    resp = await client.post("/api/v1/todos/t1/accept")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


@pytest.mark.asyncio
async def test_done_todo(client):
    client.fake_service.tasks["t1"] = _make_task(task_id="t1")
    resp = await client.post("/api/v1/todos/t1/done")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["completed_at"] is not None


@pytest.mark.asyncio
async def test_dismiss_todo(client):
    client.fake_service.tasks["t1"] = _make_task(task_id="t1", status="proposed")
    resp = await client.post("/api/v1/todos/t1/dismiss")
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"


@pytest.mark.asyncio
async def test_delete_todo(client):
    client.fake_service.tasks["t1"] = _make_task(task_id="t1")
    resp = await client.delete("/api/v1/todos/t1")
    assert resp.status_code == 204
    assert "t1" not in client.fake_service.tasks


@pytest.mark.asyncio
async def test_list_todos_filter_by_status(client):
    client.fake_service.tasks["t1"] = _make_task(task_id="t1", status="active")
    client.fake_service.tasks["t2"] = _make_task(task_id="t2", title="Other", status="done")
    resp = await client.get("/api/v1/todos", params={"status": "active"})
    assert resp.status_code == 200
    titles = [t["title"] for t in resp.json()["tasks"]]
    assert titles == ["Test task"]


@pytest.mark.asyncio
async def test_list_todos_invalid_status(client):
    resp = await client.get("/api/v1/todos", params={"status": "garbage"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_todo_no_fields(client):
    client.fake_service.tasks["t1"] = _make_task(task_id="t1")
    resp = await client.patch("/api/v1/todos/t1", json={})
    assert resp.status_code == 400


# --------------------------------------------------------------- projects

@pytest.mark.asyncio
async def test_create_and_list_project(client):
    resp = await client.post("/api/v1/projects", json={"name": "Beacon"})
    assert resp.status_code == 201
    p = resp.json()
    assert p["name"] == "Beacon"
    assert p["slug"]  # auto-derived

    list_resp = await client.get("/api/v1/projects")
    assert list_resp.status_code == 200
    assert len(list_resp.json()["projects"]) == 1


@pytest.mark.asyncio
async def test_get_project_not_found(client):
    resp = await client.get("/api/v1/projects/missing")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_project_tasks_404_for_missing(client):
    resp = await client.get("/api/v1/projects/missing/tasks")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_project_tasks_filters(client):
    client.fake_service.projects["p1"] = _make_project(project_id="p1")
    client.fake_service.tasks["t1"] = _make_task(task_id="t1", project_id="p1")
    client.fake_service.tasks["t2"] = _make_task(task_id="t2", project_id="other")
    resp = await client.get("/api/v1/projects/p1/tasks")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()["tasks"]]
    assert ids == ["t1"]


@pytest.mark.asyncio
async def test_delete_project_detaches_tasks(client):
    client.fake_service.projects["p1"] = _make_project(project_id="p1")
    client.fake_service.tasks["t1"] = _make_task(task_id="t1", project_id="p1")
    resp = await client.delete("/api/v1/projects/p1")
    assert resp.status_code == 204
    assert client.fake_service.tasks["t1"].project_id is None
