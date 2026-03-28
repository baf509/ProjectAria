"""
ARIA - Dream Cycle Routes

Purpose: API endpoints for the dream cycle / reflection engine.
View journal entries, trigger dreams, review soul proposals.
"""

from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from aria.api.deps import get_db

router = APIRouter()


# --- Response models ---

class JournalEntry(BaseModel):
    id: str
    journal_entry: str
    connections: list = []
    knowledge_gaps: list = []
    soul_proposals: list = []
    memory_consolidations_proposed: int = 0
    created_at: datetime


class SoulProposal(BaseModel):
    id: str
    proposals: list
    status: str
    created_at: datetime
    reviewed_at: Optional[datetime] = None


class DreamStatusResponse(BaseModel):
    enabled: bool
    running: bool
    interval_hours: int
    active_hours: dict
    claude_binary: str
    claude_model: str
    timeout_seconds: int
    last_run: Optional[str] = None
    last_status: Optional[str] = None
    is_active_hours: bool


# --- Endpoints ---

@router.get("/dreams/journal", response_model=list[JournalEntry])
async def list_journal_entries(
    limit: int = Query(20, ge=1, le=100),
    skip: int = Query(0, ge=0),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List dream journal entries, newest first."""
    entries = await db.dream_journal.find(
        {},
    ).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)

    return [
        JournalEntry(
            id=str(e["_id"]),
            journal_entry=e.get("journal_entry", ""),
            connections=e.get("connections", []),
            knowledge_gaps=e.get("knowledge_gaps", []),
            soul_proposals=e.get("soul_proposals", []),
            memory_consolidations_proposed=e.get("memory_consolidations_proposed", 0),
            created_at=e["created_at"],
        )
        for e in entries
    ]


@router.get("/dreams/journal/{entry_id}", response_model=JournalEntry)
async def get_journal_entry(
    entry_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get a specific journal entry."""
    try:
        oid = ObjectId(entry_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid entry ID")

    entry = await db.dream_journal.find_one({"_id": oid})
    if not entry:
        raise HTTPException(status_code=404, detail="Journal entry not found")

    return JournalEntry(
        id=str(entry["_id"]),
        journal_entry=entry.get("journal_entry", ""),
        connections=entry.get("connections", []),
        knowledge_gaps=entry.get("knowledge_gaps", []),
        soul_proposals=entry.get("soul_proposals", []),
        memory_consolidations_proposed=entry.get("memory_consolidations_proposed", 0),
        created_at=entry["created_at"],
    )


@router.get("/dreams/soul-proposals", response_model=list[SoulProposal])
async def list_soul_proposals(
    status: str = Query("pending", pattern="^(pending|approved|rejected)$"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List soul evolution proposals from dream cycles."""
    proposals = await db.dream_soul_proposals.find(
        {"status": status},
    ).sort("created_at", -1).to_list(length=50)

    return [
        SoulProposal(
            id=str(p["_id"]),
            proposals=p.get("proposals", []),
            status=p["status"],
            created_at=p["created_at"],
            reviewed_at=p.get("reviewed_at"),
        )
        for p in proposals
    ]


@router.post("/dreams/soul-proposals/{proposal_id}/approve")
async def approve_soul_proposal(
    proposal_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Approve a soul proposal — applies the proposed changes to SOUL.md.
    This requires explicit user action; dreams never auto-modify the soul.
    """
    from aria.core.soul import soul_manager

    try:
        oid = ObjectId(proposal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid proposal ID")

    proposal_doc = await db.dream_soul_proposals.find_one({"_id": oid})
    if not proposal_doc:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal_doc["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Proposal already {proposal_doc['status']}")

    # Read current soul, apply proposals
    current_soul = soul_manager.read() or ""
    updated_soul = current_soul

    applied = []
    for prop in proposal_doc.get("proposals", []):
        section = prop.get("section", "")
        proposed = prop.get("proposed", "")
        current = prop.get("current", "")

        # Try to find and replace the current text
        if current and current in updated_soul:
            updated_soul = updated_soul.replace(current, proposed, 1)
            applied.append(section)
        else:
            # Append as a new section
            updated_soul += f"\n\n## {section}\n\n{proposed}"
            applied.append(f"{section} (appended)")

    soul_manager.write(updated_soul)

    # Mark as approved
    await db.dream_soul_proposals.update_one(
        {"_id": oid},
        {"$set": {
            "status": "approved",
            "reviewed_at": datetime.now(timezone.utc),
            "applied_sections": applied,
        }},
    )

    return {"approved": True, "applied_sections": applied}


@router.post("/dreams/soul-proposals/{proposal_id}/reject")
async def reject_soul_proposal(
    proposal_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Reject a soul proposal."""
    try:
        oid = ObjectId(proposal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid proposal ID")

    result = await db.dream_soul_proposals.update_one(
        {"_id": oid, "status": "pending"},
        {"$set": {
            "status": "rejected",
            "reviewed_at": datetime.now(timezone.utc),
        }},
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Pending proposal not found")

    return {"rejected": True}


@router.post("/dreams/trigger")
async def trigger_dream(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Manually trigger a dream cycle (ignores active hours).
    Uses Claude Code CLI — runs on subscription tokens.
    """
    from aria.dreams.service import DreamService
    service = DreamService(db)
    result = await service.trigger()
    return result


@router.get("/dreams/status", response_model=DreamStatusResponse)
async def dream_status():
    """Get current dream cycle status."""
    from aria.api.deps import _dream_service
    if _dream_service is None:
        return DreamStatusResponse(
            enabled=False,
            running=False,
            interval_hours=settings.dream_interval_hours,
            active_hours={
                "start": settings.dream_active_hours_start,
                "end": settings.dream_active_hours_end,
            },
            claude_binary=settings.claude_code_binary,
            claude_model=settings.dream_claude_model or "(default)",
            timeout_seconds=settings.dream_timeout_seconds,
            is_active_hours=False,
        )
    return DreamStatusResponse(**_dream_service.status())


from aria.config import settings
