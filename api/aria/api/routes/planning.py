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

from aria.api.deps import get_db, get_planning_service, get_task_runner
from aria.planning.extraction import TaskExtractor
from aria.planning.models import (
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
from aria.planning.service import PlanningService
from aria.tasks.runner import TaskRunner

logger = logging.getLogger(__name__)
router = APIRouter()


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

    agent = await db.agents.find_one({"_id": conv.get("agent_id")})
    llm = (agent or {}).get("llm", {})
    backend = llm.get("backend", "llamacpp")
    model = llm.get("model", "default")

    async def run_extraction():
        extractor = TaskExtractor(db)
        return await extractor.extract_from_conversation(
            conversation_id,
            llm_backend=backend,
            llm_model=model,
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
):
    projects = await service.list_projects(status=status)
    return ProjectListResponse(projects=projects)


@router.post("/projects", response_model=Project, status_code=201)
async def create_project(
    body: ProjectCreateRequest,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    return await service.create_project(body)


@router.get("/projects/{project_id}", response_model=Project)
async def get_project(
    project_id: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    proj = await service.get_project(project_id)
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
    proj = await service.update_project(project_id, body)
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
    return proj


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
):
    ok = await service.delete_project(project_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
    return None


@router.get("/projects/{project_id}/tasks", response_model=TaskListResponse)
async def list_project_tasks(
    project_id: str,
    service: Annotated[PlanningService, Depends(get_planning_service)],
    status: Optional[str] = Query(default=None),
):
    proj = await service.get_project(project_id)
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
    tasks = await service.list_tasks(status=status_filter, project_id=project_id)
    return TaskListResponse(tasks=tasks)
