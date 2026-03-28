"""
ARIA - Research Routes

Purpose: Start and inspect background research runs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from aria.api.deps import get_research_service
from aria.db.models import ResearchCreate, ResearchResponse
from aria.research.service import ResearchService

router = APIRouter(prefix="/research", tags=["research"])


def serialize_research(doc: dict) -> dict:
    """Convert a research run document to an API response payload."""
    return {
        "id": str(doc["_id"]),
        "query": doc["query"],
        "status": doc["status"],
        "task_id": doc.get("task_id"),
        "backend": doc["backend"],
        "model": doc["model"],
        "depth": doc["depth"],
        "breadth": doc["breadth"],
        "progress": doc.get("progress", {}),
        "report_text": doc.get("report_text"),
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
        "completed_at": doc.get("completed_at"),
    }


@router.post("", status_code=202)
async def start_research(
    body: ResearchCreate,
    research_service: ResearchService = Depends(get_research_service),
):
    return await research_service.start_research(
        query=body.query,
        depth=body.depth,
        breadth=body.breadth,
        model=body.model,
        backend=body.backend,
        conversation_id=body.conversation_id,
    )


@router.get("", response_model=list[ResearchResponse])
async def list_research_runs(
    research_service: ResearchService = Depends(get_research_service),
):
    runs = await research_service.list_runs()
    return [ResearchResponse(**serialize_research(run)) for run in runs]


@router.get("/{research_id}", response_model=ResearchResponse)
async def get_research_run(
    research_id: str,
    research_service: ResearchService = Depends(get_research_service),
):
    run = await research_service.get_run(research_id)
    if not run:
        raise HTTPException(status_code=404, detail="Research run not found")
    return ResearchResponse(**serialize_research(run))


@router.get("/{research_id}/report")
async def get_research_report(
    research_id: str,
    research_service: ResearchService = Depends(get_research_service),
):
    report = await research_service.get_report(research_id)
    if not report:
        raise HTTPException(status_code=404, detail="Research run not found")
    return report


@router.get("/{research_id}/learnings")
async def get_research_learnings(
    research_id: str,
    research_service: ResearchService = Depends(get_research_service),
):
    run = await research_service.get_run(research_id)
    if not run:
        raise HTTPException(status_code=404, detail="Research run not found")
    return {"research_id": research_id, "learnings": await research_service.get_learnings(research_id)}
