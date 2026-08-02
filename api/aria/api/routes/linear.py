"""
ARIA - Linear reconciliation actions (Coherence C3)

Purpose: the cockpit's keep / kill / do-now verbs over the mirrored backlog,
plus the create-ticket path (Signal → Hermes → Linear). Kill and create write
back to Linear (it stays authoritative); do-now spawns a coding session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_coding_session_manager, get_db, get_planning_service
from aria.agents.session import CodingSessionManager
from aria.config import settings
from aria.planning.service import PlanningService

router = APIRouter()


def _require_linear() -> None:
    if not settings.linear_enabled or not settings.linear_api_key:
        raise HTTPException(
            status_code=409,
            detail="Linear integration is disabled (linear_enabled=false or no API key)",
        )


def _client():
    from aria.planning.linear_sync import LinearClient
    return LinearClient()


async def _mirrored_task(db, issue_id: str) -> dict:
    task = await db.tasks.find_one(
        {"external_ref.tracker": "linear", "external_ref.id": issue_id}
    )
    if not task:
        raise HTTPException(status_code=404, detail=f"No mirrored Linear issue {issue_id}")
    return task


class LinearTicketCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    description: str = Field(default="", max_length=10000)
    project: Optional[str] = Field(
        default=None,
        description="ARIA project slug from linear_project_map; defaults to the "
        "sole mapped project when only one exists.",
    )


@router.post("/linear/tickets", status_code=201)
async def create_linear_ticket(request: LinearTicketCreateRequest):
    _require_linear()
    mapping = settings.linear_project_map or {}
    if request.project:
        linear_pid = mapping.get(request.project)
        if not linear_pid:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{request.project}' has no Linear mapping "
                f"(linear_project_map keys: {sorted(mapping)})",
            )
    elif len(mapping) == 1:
        linear_pid = next(iter(mapping.values()))
    else:
        raise HTTPException(
            status_code=422,
            detail=f"Specify project — mapped: {sorted(mapping) or 'none'}",
        )
    issue = await _client().create_issue(linear_pid, request.title, request.description)
    return {"issue": issue}


@router.post("/linear/issues/{issue_id}/resolve")
async def resolve_linear_issue(
    issue_id: str,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Kill: mark the ticket done in Linear (reversible there) and complete
    the mirrored task. Also the one-tap confirm for a proposed disposition."""
    _require_linear()
    task = await _mirrored_task(db, issue_id)
    ok = await _client().resolve_issue(issue_id)
    if not ok:
        raise HTTPException(status_code=502, detail="Linear did not confirm the update")
    now = datetime.now(timezone.utc)
    await db.tasks.update_one(
        {"_id": task["_id"]},
        {
            "$set": {"status": "done", "completed_at": now, "updated_at": now},
            "$unset": {"proposed_disposition": ""},
        },
    )
    return {"resolved": True, "issue_id": issue_id}


@router.post("/linear/issues/{issue_id}/keep")
async def keep_linear_issue(
    issue_id: str,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Keep: clear any proposed disposition and pause re-judging for a while —
    an explicit human 'this stays open' decision."""
    task = await _mirrored_task(db, issue_id)
    now = datetime.now(timezone.utc)
    await db.tasks.update_one(
        {"_id": task["_id"]},
        {
            "$set": {"reconcile.kept_at": now, "updated_at": now},
            "$unset": {"proposed_disposition": ""},
        },
    )
    return {"kept": True, "issue_id": issue_id}


class LinearDoNowRequest(BaseModel):
    backend: Optional[str] = None
    loop: bool = False


@router.post("/linear/issues/{issue_id}/do-now", status_code=202)
async def do_linear_issue_now(
    issue_id: str,
    request: LinearDoNowRequest,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    planning: Annotated[PlanningService, Depends(get_planning_service)],
    sessions: Annotated[CodingSessionManager, Depends(get_coding_session_manager)],
):
    """Do-now: spawn a coding session on the ticket, in its project's
    workspace (complexity routing applies unless a backend/model pins it)."""
    task = await _mirrored_task(db, issue_id)
    project = await planning.get_project(task.get("project_id") or "")
    workspace = getattr(project, "path", None)
    if not workspace:
        raise HTTPException(
            status_code=422,
            detail="Mirrored task has no project workspace to run in",
        )
    ref = task.get("external_ref") or {}
    prompt = (
        f"Implement this Linear ticket ({ref.get('identifier') or issue_id}: "
        f"{ref.get('url') or 'no url'}).\n\nTitle: {task.get('title')}\n\n"
        f"{task.get('notes') or ''}"
    )
    try:
        session_doc = await sessions.start_session(
            workspace=workspace,
            backend=request.backend,
            prompt=prompt,
            loop={"enabled": True} if request.loop else None,
        )
    except RuntimeError as exc:  # killswitch / e-stop / queue-full refusals
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from aria.api.routes.coding_sessions import serialize_session
    session = serialize_session(session_doc)
    now = datetime.now(timezone.utc)
    await db.tasks.update_one(
        {"_id": task["_id"]},
        {
            "$set": {"reconcile.do_now_session_id": session["id"], "updated_at": now},
            "$unset": {"proposed_disposition": ""},
        },
    )
    return {"session": session, "issue_id": issue_id}
