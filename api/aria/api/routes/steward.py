"""
ARIA - Steward Routes

Purpose: the operator surface for the per-project steward — what it is set up to
do (`/steward/status`), what it actually did (`/steward/runs`), and a way to
make it do it now (`POST /steward/projects/{slug}/tick`).

The read routes are cheap and side-effect-free so the cockpit and Hermes can
poll them. The two mutating routes sit behind **ADMIN_KEY**, not the global
API_KEY: a tick at autonomy A2 spawns a sandboxed coding agent and debits a real
budget, and anything running as `ben` — including a coding agent — can read
API_KEY out of `.env`. A steward that an agent can trigger against itself is not
a steward (proposal §7.4, principle 12).

Related Spec Sections:
- ARIA_PROJECT_STEWARD_PROPOSAL_20260815.md §3.1 #9, §7.4 (key split)
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_db, get_notification_service, get_planning_service, require_admin
from aria.planning.service import PlanningService
from aria.steward.service import StewardWorker

logger = logging.getLogger(__name__)

router = APIRouter()

# Process-wide fallback for the case where main.py has not published a worker on
# app.state (steward disabled, or a test app that only mounts this router).
_steward: Optional[StewardWorker] = None


async def get_steward(
    request: Request,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    planning: Annotated[PlanningService, Depends(get_planning_service)],
) -> StewardWorker:
    """The ONE StewardWorker in this process.

    Same identity rule as the VaultReader dependency, and for the same reason:
    the worker holds tick state and is the object `VaultReader.on_events` is
    bound to, so a second instance would apply Ben's vault edits into a
    different object than the one main.py wired up.
    """
    global _steward
    worker = getattr(request.app.state, "steward", None)
    if worker is None:
        if _steward is None:
            _steward = StewardWorker(
                db, planning=planning, notifier=get_notification_service()
            )
        worker = _steward
        request.app.state.steward = worker
    if worker.db is None:
        worker.db = db
    return worker


class TickRequest(BaseModel):
    dry_run: bool = Field(
        default=False,
        description="Observe, ask the model and choose actions, but execute "
                    "nothing and write nothing (no tasks, no sessions, no vault "
                    "write, no run record).",
    )


@router.get("/steward/status")
async def steward_status(
    steward: Annotated[StewardWorker, Depends(get_steward)],
):
    """Is the steward on, what is in its active set, and where does each project
    stand? Answers honestly — and cheaply — when there are no charters at all,
    which is this box's current state."""
    return await steward.status()


@router.get("/steward/runs")
async def steward_runs(
    steward: Annotated[StewardWorker, Depends(get_steward)],
    slug: Optional[str] = None,
    limit: int = 20,
):
    """Newest-first tick history: what it saw, what it chose, and why."""
    runs = await steward.recent_runs(slug=slug, limit=max(1, min(int(limit), 200)))
    return {"runs": runs, "count": len(runs), "slug": slug}


@router.post("/steward/projects/{slug}/tick")
async def steward_tick_project(
    slug: str,
    body: TickRequest,
    steward: Annotated[StewardWorker, Depends(get_steward)],
    planning: Annotated[PlanningService, Depends(get_planning_service)],
    _admin: Annotated[bool, Depends(require_admin)],
):
    """Run one steward tick for one project, now.

    Deliberately NOT gated on `steward_enabled`: that flag governs the unattended
    timer, and an operator asking for a tick by hand is the opposite of
    unattended. Everything else still applies — autonomy, the vault approval
    gate, and the budget.
    """
    project = await planning.get_project_by_ident(slug)
    if project is None:
        raise HTTPException(status_code=404, detail=f"No project '{slug}'")
    charter = project.charter
    if not (charter and charter.purpose.strip()):
        # The active set requires a purpose because it is the text every plan and
        # research question is derived from. Refusing here is clearer than
        # returning a tick that could only ever say "nothing to do".
        raise HTTPException(
            status_code=409,
            detail=f"Project '{slug}' has no charter purpose — nothing to steward. "
                   f"Set one via PUT /projects/{slug}/charter or the vault CHARTER.md.",
        )
    if project.status != "active" or project.kind != "project":
        raise HTTPException(
            status_code=409,
            detail=f"Project '{slug}' is status={project.status} kind={project.kind}; "
                   "the active set is status=active AND kind=project.",
        )
    run = await steward.tick_project(project, dry_run=body.dry_run, trigger="manual")
    return run


@router.post("/steward/projects/{slug}/resume")
async def steward_resume_project(
    slug: str,
    steward: Annotated[StewardWorker, Depends(get_steward)],
    _admin: Annotated[bool, Depends(require_admin)],
):
    """Clear a stand-down (`steward.paused_reason`) so ticks resume.

    The steward stands itself down when it proposes pausing a project; without
    this route that proposal is a one-way door, because `paused_reason` is
    worker-owned state with no other human surface.
    """
    result = await steward.resume(slug)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No project '{slug}'")
    return result
