"""
ARIA - Complexity Routing Routes

Purpose: Expose the coding-task complexity router so thin clients on any machine
         (the desk-path `claude` wrapper on corsair or the MacBook) can ask
         "what model should this run on?" without a local venv.

These endpoints inherit the global X-API-Key auth (api_key_middleware in main.py).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.agents.routing import (
    CLAUDE_PROVIDER,
    ComplexityRouter,
    clear_cooldown,
    get_cooldown,
    record_quota_exhaustion,
)
from aria.api.deps import get_db
from aria.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/routing", tags=["routing"])


class ClassifyRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    workspace: Optional[str] = None
    # The desk path lets the judge answer a `light` task inline instead of it
    # becoming a session. Background callers leave this false.
    allow_inline_answer: bool = False


class ClassifyResponse(BaseModel):
    tier: str
    backend: str
    model: Optional[str] = None
    llm: Optional[str] = None
    why: str
    confidence: float
    source: str
    answer: Optional[str] = None
    judge_model: Optional[str] = None
    enabled: bool = True


@router.post("/classify", response_model=ClassifyResponse)
async def classify_task(
    body: ClassifyRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Classify a coding task and return the backend/model to run it on.

    Never fails on a judge error — degrades to the standard tier so a caller can
    always launch something.
    """
    if not settings.coding_routing_enabled:
        return ClassifyResponse(
            tier="standard",
            backend="claude_code",
            model=settings.coding_routing_model_standard,
            why="routing disabled",
            confidence=0.0,
            source="default",
            enabled=False,
        )
    verdict = await ComplexityRouter(db).classify(
        body.prompt,
        workspace=body.workspace,
        allow_inline_answer=body.allow_inline_answer,
    )
    return ClassifyResponse(**verdict.to_dict(), enabled=True)


class CooldownRequest(BaseModel):
    provider: str = CLAUDE_PROVIDER
    minutes: Optional[int] = None
    reason: str = "manually reported"


@router.get("/availability")
async def routing_availability(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Current provider availability — which tiers the router can still reach."""
    cooled_until = await get_cooldown(db, CLAUDE_PROVIDER)
    doc = await db.model_availability.find_one({"_id": CLAUDE_PROVIDER}) or {}
    return {
        "provider": CLAUDE_PROVIDER,
        "available": cooled_until is None,
        "cooled_until": cooled_until,
        "reason": doc.get("reason"),
        "detected_at": doc.get("detected_at"),
        "fallback": {
            "backend": settings.coding_routing_fallback_backend,
            "llm": settings.coding_routing_fallback_llm,
            "model": settings.coding_routing_fallback_model,
        },
    }


@router.post("/availability/cooldown")
async def set_cooldown(
    body: CooldownRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Manually mark a provider as quota-exhausted (the watchdog does this
    automatically when it sees quota text in a session's output)."""
    cooled_until = await record_quota_exhaustion(
        db, body.provider, minutes=body.minutes, reason=body.reason
    )
    return {"provider": body.provider, "cooled_until": cooled_until}


@router.delete("/availability/cooldown")
async def lift_cooldown(
    provider: str = CLAUDE_PROVIDER,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Lift a cooldown early — the quota reset sooner than the window assumed."""
    await clear_cooldown(db, provider)
    return {"provider": provider, "available": True}
