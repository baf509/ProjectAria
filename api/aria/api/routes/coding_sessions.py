"""
ARIA - Coding Session Routes

Purpose: Manage external coding-agent subprocess sessions.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aria.agents.review import CodingReviewService
from aria.agents.watchdog import CodingWatchdog
from aria.agents.session import CodingSessionManager
from aria.api.deps import get_coding_review_service, get_coding_session_manager, get_coding_watchdog
from aria.db.models import CodingSessionCreate, CodingSessionInput, CodingSessionResponse

router = APIRouter(prefix="/coding/sessions", tags=["coding"])


class SessionDeadlineRequest(BaseModel):
    minutes: int


def serialize_session(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "backend": doc["backend"],
        "model": doc.get("model"),
        "workspace": doc["workspace"],
        "prompt": doc["prompt"],
        "branch": doc.get("branch"),
        "pid": doc.get("pid"),
        "status": doc["status"],
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
        "completed_at": doc.get("completed_at"),
    }


@router.post("", response_model=CodingSessionResponse, status_code=201)
async def start_coding_session(
    body: CodingSessionCreate,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    session = await manager.start_session(
        workspace=body.workspace,
        backend=body.backend,
        prompt=body.prompt,
        branch=body.branch,
        model=body.model,
    )
    return CodingSessionResponse(**serialize_session(session))


@router.get("", response_model=list[CodingSessionResponse])
async def list_coding_sessions(
    status: str | None = None,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    sessions = await manager.list_sessions(status=status)
    return [CodingSessionResponse(**serialize_session(session)) for session in sessions]


@router.get("/{session_id}", response_model=CodingSessionResponse)
async def get_coding_session(
    session_id: str,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    session = await manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Coding session not found")
    return CodingSessionResponse(**serialize_session(session))


@router.get("/{session_id}/output")
async def get_coding_output(
    session_id: str,
    lines: int = 50,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    session = await manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Coding session not found")
    return {"session_id": session_id, "output": manager.get_output(session_id, lines=lines)}


@router.post("/{session_id}/input")
async def send_to_coding_session(
    session_id: str,
    body: CodingSessionInput,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    success = await manager.send_input(session_id, body.text)
    if not success:
        raise HTTPException(status_code=404, detail="Coding session not running")
    return {"session_id": session_id, "sent": True}


@router.post("/{session_id}/stop")
async def stop_coding_session(
    session_id: str,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    success = await manager.stop_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Coding session not found")
    return {"session_id": session_id, "stopped": True}


@router.get("/{session_id}/diff")
async def get_coding_diff(
    session_id: str,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    session = await manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Coding session not found")
    return {"session_id": session_id, "diff": await manager.get_diff(session_id)}


@router.post("/{session_id}/review")
async def review_coding_session(
    session_id: str,
    review_service: CodingReviewService = Depends(get_coding_review_service),
):
    try:
        return await review_service.review_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{session_id}/review")
async def get_coding_review(
    session_id: str,
    review_service: CodingReviewService = Depends(get_coding_review_service),
):
    report = await review_service.get_report(session_id)
    if not report:
        raise HTTPException(status_code=404, detail="Review report not found")
    return report


@router.post("/watchdog/start")
async def start_coding_watchdog(
    watchdog: CodingWatchdog = Depends(get_coding_watchdog),
):
    return await watchdog.start()


@router.post("/watchdog/stop")
async def stop_coding_watchdog(
    watchdog: CodingWatchdog = Depends(get_coding_watchdog),
):
    return await watchdog.stop()


@router.get("/watchdog/status")
async def get_coding_watchdog_status(
    watchdog: CodingWatchdog = Depends(get_coding_watchdog),
):
    return watchdog.status()


@router.post("/{session_id}/deadline")
async def set_coding_deadline(
    session_id: str,
    body: SessionDeadlineRequest,
    watchdog: CodingWatchdog = Depends(get_coding_watchdog),
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    session = await manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Coding session not found")
    await watchdog.set_deadline(session_id, body.minutes)
    return {"session_id": session_id, "deadline_minutes": body.minutes}
