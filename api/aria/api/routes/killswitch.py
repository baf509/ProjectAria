"""
ARIA - Killswitch Routes

Purpose: API endpoints for emergency stop activation and status.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from aria.api.deps import get_killswitch, get_task_runner, get_notification_service
from aria.core.killswitch import Killswitch
from aria.tasks.runner import TaskRunner
from aria.notifications.service import NotificationService

router = APIRouter()


class KillswitchActivateRequest(BaseModel):
    reason: str = "Manual activation"


@router.post("/killswitch/activate")
async def activate_killswitch(
    body: KillswitchActivateRequest = KillswitchActivateRequest(),
    killswitch: Killswitch = Depends(get_killswitch),
    task_runner: TaskRunner = Depends(get_task_runner),
    notification_service: NotificationService = Depends(get_notification_service),
):
    """Activate the emergency killswitch, cancelling all autonomous operations."""
    return await killswitch.activate(
        body.reason,
        task_runner=task_runner,
        notification_service=notification_service,
    )


@router.post("/killswitch/deactivate")
async def deactivate_killswitch(
    killswitch: Killswitch = Depends(get_killswitch),
):
    """Deactivate the killswitch, allowing operations to resume."""
    return await killswitch.deactivate()


@router.get("/killswitch/status")
async def killswitch_status(
    killswitch: Killswitch = Depends(get_killswitch),
):
    """Get current killswitch status."""
    return killswitch.status()
