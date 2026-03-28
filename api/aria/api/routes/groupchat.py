"""
ARIA - Group Chat Routes

Purpose: API endpoints for multi-persona debate sessions.
"""

import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from aria.api.deps import get_groupchat_service
from aria.groupchat.service import GroupChatService

router = APIRouter()


class GroupChatCreateRequest(BaseModel):
    question: str
    persona_ids: list[str]
    rounds: int = 0
    synthesis: bool = True


@router.post("/groupchat/sessions")
async def create_groupchat_session(
    body: GroupChatCreateRequest,
    service: GroupChatService = Depends(get_groupchat_service),
):
    """Create a new group chat debate session."""
    try:
        return await service.create_session(
            question=body.question,
            persona_ids=body.persona_ids,
            rounds=body.rounds,
            synthesis=body.synthesis,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/groupchat/sessions/{session_id}/stream")
async def stream_groupchat(
    session_id: str,
    service: GroupChatService = Depends(get_groupchat_service),
):
    """Stream a group chat debate via SSE."""

    async def event_generator():
        event_id = 1
        async for chunk in service.run_debate(session_id):
            yield {
                "id": str(event_id),
                "event": chunk.type,
                "data": json.dumps(chunk.to_dict()),
            }
            event_id += 1

    return EventSourceResponse(
        event_generator(),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/groupchat/sessions/{session_id}")
async def get_groupchat_transcript(
    session_id: str,
    service: GroupChatService = Depends(get_groupchat_service),
):
    """Get the transcript for a group chat session."""
    result = await service.get_transcript(session_id)
    if not result:
        raise HTTPException(status_code=404, detail="Session not found")
    return result
