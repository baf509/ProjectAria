"""
ARIA - Awareness API Routes

Purpose: Status, manual triggers, and observation queries for the
ambient awareness system.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from aria.api.deps import get_awareness_service, get_db
from aria.awareness.service import AwarenessService
from aria.awareness.triggers import TriggerRule

router = APIRouter()


class ObservationResponse(BaseModel):
    sensor: str
    category: str
    event_type: str
    summary: str
    detail: Optional[str] = None
    severity: str
    tags: list[str] = []
    created_at: datetime


class AwarenessStatusResponse(BaseModel):
    enabled: bool
    running: bool
    sensors: list[str]
    poll_interval_seconds: int
    analysis_interval_minutes: int
    observation_ttl_hours: int
    watch_dirs: list[str]
    last_poll: Optional[str] = None
    last_analysis: Optional[str] = None


@router.get("/awareness/status", response_model=AwarenessStatusResponse)
async def awareness_status(
    service: AwarenessService = Depends(get_awareness_service),
):
    """Get current awareness service status."""
    return AwarenessStatusResponse(**service.status())


@router.get("/awareness/observations", response_model=list[ObservationResponse])
async def list_observations(
    limit: int = Query(20, ge=1, le=100),
    category: Optional[str] = Query(None, pattern="^(git|system|filesystem)$"),
    severity: Optional[str] = Query(None, pattern="^(info|notice|warning)$"),
    hours: float = Query(1.0, ge=0.1, le=168),
    service: AwarenessService = Depends(get_awareness_service),
):
    """Query recent observations from environmental sensors."""
    observations = await service.get_recent_observations(
        limit=limit, category=category, severity=severity, hours=hours,
    )
    return [
        ObservationResponse(
            sensor=o["sensor"],
            category=o["category"],
            event_type=o["event_type"],
            summary=o["summary"],
            detail=o.get("detail"),
            severity=o.get("severity", "info"),
            tags=o.get("tags", []),
            created_at=o["created_at"],
        )
        for o in observations
    ]


@router.get("/awareness/summary")
async def latest_summary(
    service: AwarenessService = Depends(get_awareness_service),
):
    """Get the most recent awareness analysis summary."""
    summary = await service.get_latest_summary()
    return {"summary": summary}


@router.post("/awareness/poll")
async def trigger_poll(
    service: AwarenessService = Depends(get_awareness_service),
):
    """Manually trigger a sensor poll cycle."""
    return await service.trigger_poll()


@router.post("/awareness/analyze")
async def trigger_analysis(
    service: AwarenessService = Depends(get_awareness_service),
):
    """Manually trigger an awareness analysis (uses ClaudeRunner)."""
    return await service.trigger_analysis()


# ---- Event-Driven Trigger Rules ----


class TriggerRuleCreate(BaseModel):
    name: str
    category: str
    severity: Optional[str] = None
    content_pattern: Optional[str] = None
    action: str = "notify"  # "notify" | "tool" | "prompt"
    action_params: dict = {}
    cooldown_seconds: int = 300
    enabled: bool = True


@router.get("/awareness/triggers")
async def list_triggers(
    service: AwarenessService = Depends(get_awareness_service),
):
    """List all event-driven awareness trigger rules."""
    return {"triggers": service.trigger_engine.list_rules()}


@router.post("/awareness/triggers", status_code=201)
async def create_trigger(
    body: TriggerRuleCreate,
    service: AwarenessService = Depends(get_awareness_service),
):
    """Create or update an event-driven trigger rule.

    When an observation matches the rule's criteria (category, severity,
    content_pattern), the specified action fires automatically.
    """
    rule = TriggerRule(
        name=body.name,
        category=body.category,
        severity=body.severity,
        content_pattern=body.content_pattern,
        action=body.action,
        action_params=body.action_params,
        cooldown_seconds=body.cooldown_seconds,
        enabled=body.enabled,
    )
    name = await service.trigger_engine.add_rule(rule)
    return {"name": name, "status": "created"}


@router.delete("/awareness/triggers/{name}")
async def delete_trigger(
    name: str,
    service: AwarenessService = Depends(get_awareness_service),
):
    """Delete an event-driven trigger rule."""
    removed = await service.trigger_engine.remove_rule(name)
    if not removed:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Trigger rule not found")
    return {"status": "deleted"}
