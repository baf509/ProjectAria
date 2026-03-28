"""
ARIA - Workflow Routes

Purpose: CRUD and execution APIs for workflows.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from aria.api.deps import get_workflow_engine
from aria.workflows.engine import WorkflowEngine

router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowStepRequest(BaseModel):
    action: str
    params: dict = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)


class WorkflowCreateRequest(BaseModel):
    name: str
    description: str = ""
    steps: list[WorkflowStepRequest]
    tags: list[str] = Field(default_factory=list)


class WorkflowRunRequest(BaseModel):
    dry_run: bool = False


@router.get("")
async def list_workflows(engine: WorkflowEngine = Depends(get_workflow_engine)):
    return await engine.list_workflows()


@router.post("", status_code=201)
async def create_workflow(body: WorkflowCreateRequest, engine: WorkflowEngine = Depends(get_workflow_engine)):
    return await engine.create_workflow(body.model_dump())


@router.post("/{workflow_id}/run")
async def run_workflow(
    workflow_id: str,
    body: WorkflowRunRequest,
    engine: WorkflowEngine = Depends(get_workflow_engine),
):
    try:
        return await engine.run_workflow(workflow_id, dry_run=body.dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: str, engine: WorkflowEngine = Depends(get_workflow_engine)):
    from aria.api.deps import valid_object_id

    result = await engine.db.workflows.delete_one({"_id": valid_object_id(workflow_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return None


@router.get("/{workflow_id}/status")
async def workflow_status(workflow_id: str, engine: WorkflowEngine = Depends(get_workflow_engine)):
    workflow = await engine.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    runs = await engine.db.workflow_runs.find({"workflow_id": workflow_id}).sort("created_at", -1).to_list(length=20)
    return {"workflow": workflow, "runs": runs}
