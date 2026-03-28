"""
ARIA - Schedule Routes

Phase: 14
Purpose: CRUD API for scheduled tasks and reminders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from aria.api.deps import get_scheduler
from aria.scheduler.service import SchedulerService

router = APIRouter(prefix="/schedules", tags=["schedules"])


class CreateScheduleRequest(BaseModel):
    name: str = Field(..., description="Human-readable schedule name")
    schedule_type: str = Field(..., description="'once' or 'recurring'")
    action: str = Field(..., description="'remind', 'prompt', 'tool', or 'notify'")
    params: dict = Field(default_factory=dict, description="Action-specific parameters")
    cron_expr: Optional[str] = Field(None, description="Simplified cron expression for recurring schedules")
    run_at: Optional[datetime] = Field(None, description="ISO datetime for one-shot schedules (UTC)")


class ToggleScheduleRequest(BaseModel):
    enabled: bool


def _serialize_schedule(doc: dict) -> dict:
    """Convert a MongoDB schedule document to a JSON-safe dict."""
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    for key in ("next_run_at", "last_run_at", "created_at", "updated_at"):
        if key in doc and isinstance(doc[key], datetime):
            doc[key] = doc[key].isoformat()
    return doc


@router.get("")
async def list_schedules(
    enabled_only: bool = False,
    scheduler: SchedulerService = Depends(get_scheduler),
):
    """List all schedules."""
    schedules = await scheduler.list_schedules(enabled_only=enabled_only)
    return [_serialize_schedule(s) for s in schedules]


@router.post("", status_code=201)
async def create_schedule(
    body: CreateScheduleRequest,
    scheduler: SchedulerService = Depends(get_scheduler),
):
    """Create a new schedule or one-shot reminder."""
    try:
        schedule_id = await scheduler.create_schedule(
            name=body.name,
            schedule_type=body.schedule_type,
            action=body.action,
            params=body.params,
            cron_expr=body.cron_expr,
            run_at=body.run_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": schedule_id}


@router.get("/{schedule_id}")
async def get_schedule(
    schedule_id: str,
    scheduler: SchedulerService = Depends(get_scheduler),
):
    """Get a single schedule by ID."""
    doc = await scheduler.get_schedule(schedule_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return _serialize_schedule(doc)


@router.delete("/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    scheduler: SchedulerService = Depends(get_scheduler),
):
    """Delete a schedule."""
    deleted = await scheduler.delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"id": schedule_id, "deleted": True}


@router.patch("/{schedule_id}/toggle")
async def toggle_schedule(
    schedule_id: str,
    body: ToggleScheduleRequest,
    scheduler: SchedulerService = Depends(get_scheduler),
):
    """Enable or disable a schedule."""
    updated = await scheduler.toggle_schedule(schedule_id, body.enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"id": schedule_id, "enabled": body.enabled}


class ParseReminderRequest(BaseModel):
    text: str


@router.post("/parse-reminder")
async def parse_reminder(
    body: ParseReminderRequest,
    scheduler: SchedulerService = Depends(get_scheduler),
):
    """Parse a natural language reminder and return the schedule parameters."""
    result = await scheduler.parse_reminder(body.text)
    if result is None:
        raise HTTPException(status_code=422, detail="Could not parse reminder from text")
    # Convert datetime to ISO string for JSON
    if "run_at" in result and isinstance(result["run_at"], datetime):
        result["run_at"] = result["run_at"].isoformat()
    return result
