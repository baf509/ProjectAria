"""Operator-approved Loop plans and control, using Aria's existing key split."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from aria.api.deps import get_db, require_admin
from aria.loop.git import OwnershipError
from aria.loop.models import CreateRun, Limits, Plan, StrictModel
from aria.loop.service import LoopService, StopRun


router = APIRouter(prefix="/loop")


async def get_loop(request: Request, db=Depends(get_db)):
    service = getattr(request.app.state, "loop", None)
    if service is None:
        service = LoopService(db)
        request.app.state.loop = service
    return service


class Approval(StrictModel):
    expected_version: int


class PlanUpdate(Approval):
    plan: Plan


class LimitsUpdate(Approval):
    limits: Limits
    handoff: str | None = Field(default=None, max_length=2000)


async def call(operation):
    try:
        return await operation
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OwnershipError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError, StopRun) as exc:
        raise HTTPException(400, getattr(exc, "reason", str(exc))) from exc


@router.get("/policy")
async def policy(service=Depends(get_loop)):
    from pathlib import Path
    from aria.loop.config import Policy
    path = Path(service.settings.policy_file).expanduser()
    if not path.is_file():
        return {"enabled": service.settings.enabled, "projects": {}, "configuration_required": True}
    config = Policy.model_validate_json(path.read_text())
    return {"enabled": service.settings.enabled, "projects": {
        slug: {"check_ids": list(p.checks), "regression_check_ids": p.regression_check_ids,
               "final_check_ids": p.final_check_ids, "protected_paths": p.protected_paths,
               "allowed_backends": p.allowed_backends} for slug, p in config.projects.items()}}


@router.get("/runs")
async def list_runs(service=Depends(get_loop)):
    return await service.store.runs.find({}, {"attempts": 0, "events": 0}).sort("created_at", -1).limit(100).to_list(100)


@router.post("/runs", dependencies=[Depends(require_admin)], status_code=201)
async def create_run(body: CreateRun, service=Depends(get_loop)):
    return await call(service.create(body))


@router.get("/runs/{run_id}")
async def inspect_run(run_id: str, service=Depends(get_loop)):
    return await call(service.inspect(run_id))


@router.put("/runs/{run_id}/plan", dependencies=[Depends(require_admin)])
async def update_plan(run_id: str, body: PlanUpdate, service=Depends(get_loop)):
    return await call(service.replace_plan(run_id, body.plan.model_dump(), body.expected_version))


@router.post("/runs/{run_id}/approve", dependencies=[Depends(require_admin)])
async def approve_run(run_id: str, body: Approval, service=Depends(get_loop)):
    return await call(service.approve(run_id, body.expected_version))


@router.put("/runs/{run_id}/limits", dependencies=[Depends(require_admin)])
async def extend_limits(run_id: str, body: LimitsUpdate, service=Depends(get_loop)):
    return await call(service.extend_limits(run_id, body.limits.model_dump(), body.expected_version, body.handoff))


@router.post("/runs/{run_id}/{action}", dependencies=[Depends(require_admin)])
async def control_run(run_id: str, action: str, service=Depends(get_loop)):
    if action == "recover":
        return await call(service.recover(run_id))
    return await call(service.control(run_id, action))


@router.get("/runs/{run_id}/logs")
async def logs(run_id: str, attempt_id: str | None = None, offset: int = Query(0, ge=0),
               limit: int = Query(50, ge=1, le=100), service=Depends(get_loop)):
    await call(service.store.get(run_id))
    query = {"run_id": run_id}
    if attempt_id:
        query["attempt_id"] = attempt_id
    rows = await service.db.loop_logs.find(query).sort("at", 1).skip(offset).limit(limit).to_list(limit)
    for row in rows:
        row["_id"] = str(row["_id"])
    return rows
