"""
ARIA - Autopilot Routes

Purpose: API endpoints for autopilot mode with safety tiers.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from aria.api.deps import get_autopilot_service
from aria.autopilot.service import AutopilotService

router = APIRouter()


class AutopilotStartRequest(BaseModel):
    goal: str
    mode: str = "safe"
    backend: str = "llamacpp"
    model: str = "default"
    context: str = ""


class AutopilotApproveRequest(BaseModel):
    step_index: int


@router.post("/autopilot/start")
async def start_autopilot(
    body: AutopilotStartRequest,
    service: AutopilotService = Depends(get_autopilot_service),
):
    """Start an autopilot session with a goal."""
    try:
        return await service.start(
            goal=body.goal,
            mode=body.mode,
            backend=body.backend,
            model=body.model,
            context=body.context,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/autopilot/sessions/{session_id}/approve")
async def approve_autopilot_step(
    session_id: str,
    body: AutopilotApproveRequest,
    service: AutopilotService = Depends(get_autopilot_service),
):
    """Approve a step in safe mode."""
    approved = service.approve_step(session_id, body.step_index)
    if not approved:
        raise HTTPException(
            status_code=404,
            detail="No pending approval for this step",
        )
    return {"approved": True, "session_id": session_id, "step_index": body.step_index}


@router.get("/autopilot/sessions/{session_id}")
async def get_autopilot_session(
    session_id: str,
    service: AutopilotService = Depends(get_autopilot_service),
):
    """Get autopilot session details and progress."""
    session = await service.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/autopilot/sessions/{session_id}/stop")
async def stop_autopilot(
    session_id: str,
    service: AutopilotService = Depends(get_autopilot_service),
):
    """Stop an autopilot session."""
    try:
        return await service.stop(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
