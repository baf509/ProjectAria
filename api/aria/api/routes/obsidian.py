"""
ARIA - Obsidian publish endpoint (Coherence C6)

Purpose: let agents (Hermes via MCP, coding sub-agents, workflows) publish
long-form markdown — analyses, design drafts, reports — into the Obsidian
vault's per-project folders through the guarded ObsidianWriter, instead of
raw filesystem writes.
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_db, get_vault_reader
from aria.integrations.obsidian import DOC_TYPES, ObsidianWriter

router = APIRouter()


class ObsidianPublishRequest(BaseModel):
    content: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1, max_length=300)
    doc_type: str = Field(
        default="Analysis",
        description=f"One of {DOC_TYPES} — the vault subfolder.",
    )
    project: Optional[str] = Field(
        default=None,
        description="Repo path or vault folder name; omit for the default "
        "(non-project) folder.",
    )


@router.post("/obsidian/publish")
async def publish_to_obsidian(
    request: ObsidianPublishRequest,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    # The db handle is what records the content hash of this write. Publishing
    # without it leaves the VaultReader no baseline, so ARIA's own document
    # comes back on the next poll looking like an edit by Ben.
    writer = ObsidianWriter(db=db)
    if not writer.enabled():
        raise HTTPException(
            status_code=409,
            detail="Obsidian publishing is disabled (obsidian_enabled=false) "
            "or the vault path is missing",
        )
    if request.doc_type not in DOC_TYPES:
        raise HTTPException(
            status_code=422, detail=f"doc_type must be one of {DOC_TYPES}"
        )
    path = await writer.publish(
        request.content,
        title=request.title,
        doc_type=request.doc_type,
        project=request.project,
    )
    if not path:
        raise HTTPException(status_code=500, detail="publish failed (see logs)")
    return {"path": path}


@router.post("/vault/poll")
async def poll_vault(reader=Depends(get_vault_reader)):
    """Read the vault's control docs right now and return what changed.

    The worker already does this on a timer; this exists so a human or the
    steward can force a read after telling Ben "edit the plan and I'll pick it
    up", without waiting out the interval."""
    events = await reader.poll_once()
    return {"count": len(events), "events": events, "at": reader.last_poll_at}


@router.get("/vault/events")
async def recent_vault_events(limit: int = 50, reader=Depends(get_vault_reader)):
    """Recent vault change events (in-memory ring, newest last)."""
    return {"events": reader.recent_events[-limit:], "last_poll_at": reader.last_poll_at}
