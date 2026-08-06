"""
ARIA - Benchmark Routes

Purpose: launch and inspect `evalstack` benchmark runs (code / tool-use /
performance / agents) from the UI, instead of by hand in a shell.

See aria.benchmarks.service for why evalstack is run as a subprocess and why a
run that would disturb a BOUND model server requires force=True.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_db, get_model_server_manager
from aria.benchmarks.service import BenchmarkError, BenchmarkService
from aria.infrastructure.model_servers import ModelServerManager

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])

_service = BenchmarkService()


class StartRunRequest(BaseModel):
    suites: list[str] = Field(description="Suite names, e.g. ['code','tool-use','performance']")
    targets: list[str] = Field(description="evalstack target names, e.g. ['ds4-affine']")
    run_id: Optional[str] = Field(default=None, description="Run name; auto-generated if omitted")
    limit: Optional[int] = Field(default=None, ge=1, description="Cap samples per benchmark")
    allow_coresident: bool = Field(default=False, description="Skip evalstack's VRAM guard")
    keep_up: bool = Field(default=False, description="Leave the last model server running")
    force: bool = Field(default=False,
                        description="Proceed even if a target would disturb a model "
                                    "currently bound to an agent")


def _svc() -> BenchmarkService:
    if not _service.available():
        raise HTTPException(status_code=503,
                            detail=f"evalstack not available at {_service.root}; "
                                   f"set $EVALSTACK_ROOT")
    return _service


@router.get("/health")
async def benchmarks_health():
    """Is the harness reachable, and what does it think the GPU budget is?"""
    ok = _service.available()
    out = {"available": ok, "root": str(_service.root), "binary": _service.bin}
    if ok:
        try:
            out["gpu_budget_gb"] = await _service.gpu_budget_gb()
        except Exception as ex:
            out["error"] = str(ex)
    return out


@router.get("/suites")
async def list_suites(svc: BenchmarkService = Depends(_svc)):
    """Named suites the UI offers as checkboxes."""
    try:
        return {"suites": await svc.list_suites()}
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))


@router.get("/targets")
async def list_targets(svc: BenchmarkService = Depends(_svc)):
    try:
        return {"targets": await svc.list_targets(),
                "gpu_budget_gb": await svc.gpu_budget_gb()}
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))


async def _bound_conflicts(manager: ModelServerManager, db, targets: list[str],
                           svc: BenchmarkService) -> list[str]:
    """Model servers that are bound to an agent AND are not among the requested
    targets — i.e. things a benchmark could stop out from under a live agent.

    Best-effort: ARIA's registry keys on its own slugs while evalstack keys on
    target names, so we match on the served model id where we can. A failure to
    introspect must not block benchmarking, only the *confident* conflicts do.
    """
    try:
        rows = await manager.status(db)
    except Exception:
        return []
    wanted_models = {t["model"] for t in await svc.list_targets()
                     if t["name"] in targets}
    conflicts = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        state = str(r.get("state") or r.get("status") or "").lower()
        bound = r.get("bound_agent") or r.get("bound_to") or r.get("agent")
        if not bound or "run" not in state:
            continue
        model = r.get("model") or r.get("alias") or r.get("slug")
        if model and model in wanted_models:
            continue                      # it's a target; being restarted is expected
        conflicts.append(f"{r.get('slug', model)} (bound to {bound})")
    return conflicts


@router.post("/runs")
async def start_run(
    body: StartRunRequest,
    svc: BenchmarkService = Depends(_svc),
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Start a benchmark run. Long-running: poll GET /benchmarks/runs/{run_id}.

    Benchmarks stop and start model servers, so this refuses to run while a
    *bound* model server would be disturbed unless force=True.
    """
    if not body.force:
        conflicts = await _bound_conflicts(manager, db, body.targets, svc)
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "benchmark would disturb bound model server(s)",
                    "conflicts": conflicts,
                    "hint": "unbind them, or re-POST with force=true",
                },
            )
    try:
        return await svc.start_run(
            suites=body.suites, targets=body.targets, run_id=body.run_id,
            limit=body.limit, allow_coresident=body.allow_coresident,
            keep_up=body.keep_up,
        )
    except BenchmarkError as ex:
        raise HTTPException(status_code=400, detail=str(ex))


@router.get("/runs")
async def list_runs(limit: int = 25, svc: BenchmarkService = Depends(_svc)):
    return {"runs": await svc.list_runs(limit=limit)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, tail: int = 80, svc: BenchmarkService = Depends(_svc)):
    try:
        return await svc.get_run(run_id, tail=tail)
    except BenchmarkError as ex:
        raise HTTPException(status_code=404, detail=str(ex))


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, svc: BenchmarkService = Depends(_svc)):
    try:
        return await svc.cancel(run_id)
    except BenchmarkError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
