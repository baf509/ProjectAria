"""
ARIA - Planning Routes (todos + projects)

REST API for the to-do list and long-term project tracker. Mounted under
/api/v1 with paths /todos and /projects.
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from aria.api.deps import get_db, get_planning_service, get_task_runner
from aria.config import settings
from aria.planning.extraction import TaskExtractor
from aria.planning.models import (
    CharterResponse,
    CharterSetRequest,
    Project,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectStatus,
    ProjectUpdateRequest,
    Task,
    TaskCreateRequest,
    TaskListResponse,
    TaskStatus,
    TaskUpdateRequest,
)
from aria.planning.service import (
    CharterRefused,
    PlanningService,
    active_set_blockers,
    effective_budget,
)
from aria.tasks.runner import TaskRunner

logger = logging.getLogger(__name__)
router = APIRouter()


def _charter_response(proj: Project) -> CharterResponse:
    """One builder for both charter routes, so the active-set verdict is always
    reported. A 200 that echoed the charter back and said nothing about whether
    the steward would ever read it is how a charter on an ignored row looked
    exactly like a charter on a live project."""
    blockers = active_set_blockers(proj)
    return CharterResponse(
        project_id=proj.id,
        slug=proj.slug,
        kind=proj.kind,
        charter=proj.charter,
        steward=proj.steward,
        effective_budget=effective_budget(proj.charter),
        in_active_set=not blockers,
        active_set_blockers=blockers,
    )


def _charter_conflict(exc: CharterRefused) -> HTTPException:
    """A refused charter is a 409 carrying the reason AND the fix — the caller
    is a human surface (API/MCP/vault), so 'no' has to be actionable."""
    return HTTPException(
        status_code=409,
        detail={
            "error": "charter_refused",
            "project": exc.slug,
            "reason": exc.reason,
            "remedy": exc.remedy,
        },
    )


# --------------------------------------------------------------------- todos

@router.get("/todos", response_model=TaskListResponse)
async def list_todos(
    service: Annotated[PlanningService, Depends(get_planning_service)],
    status: Optional[str] = Query(default=None, description="Comma-separated statuses"),
    project_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, le=1000),
):
    status_filter: Optional[list[TaskStatus]] = None
    if status:
        valid = {"proposed", "active", "done", "dismissed"}
        parts = [s.strip() for s in status.split(",") if s.strip()]
        bad = [p for p in parts if p not in valid]
        if bad:
            raise HTTPException(status_code=422, detail=f"Invalid status(es): {bad}")
        status_filter = parts  # type: ignore[assignment]
    tasks = await service.list_tasks(status=status_filter, project_id=project_id, limit=limit)
    return TaskListResponse(tasks=tasks)


@router.post("/todos", response_model=Task, status_code=201)
async def create_todo(
    body: TaskCreateRequest,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    return await service.create_task(body)


@router.get("/todos/{task_id}", response_model=Task)
async def get_todo(
    task_id: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    task = await service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task


@router.patch("/todos/{task_id}", response_model=Task)
async def update_todo(
    task_id: str,
    body: TaskUpdateRequest,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    if not body.model_dump(exclude_unset=True):
        raise HTTPException(status_code=400, detail="No fields to update")
    task = await service.update_task(task_id, body)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task


@router.delete("/todos/{task_id}", status_code=204)
async def delete_todo(
    task_id: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    ok = await service.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return None


@router.post("/todos/{task_id}/accept", response_model=Task)
async def accept_todo(
    task_id: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    """Promote a proposed task to active. No-op if already active."""
    task = await service.set_task_status(task_id, "active")
    if not task:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task


@router.post("/todos/{task_id}/dismiss", response_model=Task)
async def dismiss_todo(
    task_id: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    task = await service.set_task_status(task_id, "dismissed")
    if not task:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task


@router.post("/todos/{task_id}/done", response_model=Task)
async def complete_todo(
    task_id: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    task = await service.set_task_status(task_id, "done")
    if not task:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task


@router.post("/todos/extract/{conversation_id}", status_code=202)
async def extract_todos_from_conversation(
    conversation_id: str,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
):
    """Manually trigger task extraction for a conversation (background)."""
    try:
        oid = ObjectId(conversation_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid conversation_id")
    conv = await db.conversations.find_one({"_id": oid})
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    async def run_extraction():
        extractor = TaskExtractor(db)
        return await extractor.extract_from_conversation(
            conversation_id,
            llm_backend=settings.planning_ambient_backend,
            llm_model=settings.planning_ambient_model,
            private=bool(conv.get("private", False)),
        )

    task_id = await task_runner.submit_task(
        name="task_extraction",
        coroutine_factory=run_extraction,
        notify=False,
        metadata={"conversation_id": conversation_id, "task_kind": "task_extraction"},
    )
    return {
        "message": "Task extraction started",
        "conversation_id": conversation_id,
        "task_id": task_id,
    }


# ------------------------------------------------------------------ projects

@router.get("/projects", response_model=ProjectListResponse)
async def list_projects(
    service: Annotated[PlanningService, Depends(get_planning_service)],
    status: Optional[ProjectStatus] = Query(default=None),
    view: str = Query(default="full"),
):
    """Every project.

    `view=list` drops `sources` — the harvester's provenance records, which are
    57% of this payload (47.5 of 83.4 KB across 64 projects) and are not
    rendered anywhere. The Know/tasks page needs project names for attribution
    and was paying 97 KB a load for them.
    """
    projects = await service.list_projects(status=status)
    if view == "list":
        for p in projects:
            if hasattr(p, "sources"):
                p.sources = []
    return ProjectListResponse(projects=projects)


@router.post("/projects", response_model=Project, status_code=201)
async def create_project(
    body: ProjectCreateRequest,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    return await service.create_project(body)


# ⚠️ Named `active-set`, not `active`, on purpose: `GET/PUT /projects/active`
# already exists in routes/digest.py as the server-side *active project pointer*
# (one slug, the cockpit's current focus), and digest registers before planning
# in main.py precisely so its literal paths beat `/projects/{project_id}`.
# Two different concepts — one pointer, one set — so they get two paths rather
# than one path whose meaning depends on which router won.
# Declared above `/projects/{project_id}` because within a router FastAPI still
# matches in declaration order.
@router.get("/projects/active-set", response_model=ProjectListResponse)
async def list_active_set(
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    """The ACTIVE SET the steward iterates: status=active AND kind=project AND
    a charter with a non-empty purpose. Everything else in `projects` is
    inventory."""
    return ProjectListResponse(projects=await service.active_projects())


@router.get("/projects/{project_id}", response_model=Project)
async def get_project(
    project_id: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    # Accept either a Mongo ObjectId or a project slug, so agents/MCP can address
    # a project by its stable slug without first resolving the id.
    proj = await service.get_project(project_id)
    if not proj:
        proj = await service.get_project_by_slug(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
    return proj


@router.patch("/projects/{project_id}", response_model=Project)
async def update_project(
    project_id: str,
    body: ProjectUpdateRequest,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    if not body.model_dump(exclude_unset=True):
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        proj = await service.update_project(project_id, body)
    except CharterRefused as exc:
        raise _charter_conflict(exc) from exc
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
    return proj


class RetireRequest(BaseModel):
    """`dry_run` previews exactly what would move to memory and what would go."""

    dry_run: bool = False


@router.post("/projects/{ident}/retire")
async def retire_project(
    ident: str,
    body: RetireRequest,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    """Retire a project: distil its transcripts into long-term memory, then
    remove it.

    Deliberately NOT a variant of DELETE. The memories are written and verified
    first, and a failure to write anything aborts before the project is touched
    — the whole point is that the record survives the row.
    """
    from aria.planning.retirement import ProjectRetirementService, RetirementRefused

    retirer = ProjectRetirementService(db, service)
    try:
        return await retirer.retire(ident, dry_run=body.dry_run)
    except RetirementRefused as exc:
        # 409: the request is well-formed, the project's state forbids it.
        raise HTTPException(status_code=409, detail=str(exc))


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    ok = await service.delete_project(project_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
    return None


@router.get("/projects/{ident}/charter", response_model=CharterResponse)
async def get_project_charter(
    ident: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    proj = await service.get_project_by_ident(ident)
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project not found: {ident}")
    return _charter_response(proj)


@router.put("/projects/{ident}/charter", response_model=CharterResponse)
async def set_project_charter(
    ident: str,
    body: CharterSetRequest,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    """Set or amend a charter. The body is a PARTIAL charter — only the keys
    present are merged, so a vault/phone edit of one field cannot blank the
    rest. This route is a human surface (it always writes as actor `human`);
    workers call PlanningService.set_charter with their own actor and get
    propose-into-scan_review semantics instead.

    409 when the charter cannot take effect (a human marked this row
    `kind=ignored`); the body carries the reason and the one-PATCH fix."""
    patch = body.charter.model_dump(exclude_unset=True)
    try:
        proj = await service.set_charter(ident, patch, actor="human", via=body.via)
    except CharterRefused as exc:
        raise _charter_conflict(exc) from exc
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project not found: {ident}")
    return _charter_response(proj)


@router.get("/projects/{project_id}/tasks", response_model=TaskListResponse)
async def list_project_tasks(
    project_id: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
    status: Optional[str] = Query(default=None),
):
    proj = await service.get_project(project_id)
    if not proj:
        proj = await service.get_project_by_slug(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
    status_filter: Optional[list[TaskStatus]] = None
    if status:
        valid = {"proposed", "active", "done", "dismissed"}
        parts = [s.strip() for s in status.split(",") if s.strip()]
        bad = [p for p in parts if p not in valid]
        if bad:
            raise HTTPException(status_code=422, detail=f"Invalid status(es): {bad}")
        status_filter = parts  # type: ignore[assignment]
    tasks = await service.list_tasks(status=status_filter, project_id=proj.id)
    return TaskListResponse(tasks=tasks)
