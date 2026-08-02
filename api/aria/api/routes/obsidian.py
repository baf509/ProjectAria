"""
ARIA - Obsidian publish endpoint (Coherence C6)

Purpose: let agents (Hermes via MCP, coding sub-agents, workflows) publish
long-form markdown — analyses, design drafts, reports — into the Obsidian
vault's per-project folders through the guarded ObsidianWriter, instead of
raw filesystem writes.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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
async def publish_to_obsidian(request: ObsidianPublishRequest):
    writer = ObsidianWriter()
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
