"""
ARIA - Killswitch Routes

Purpose: API endpoints for emergency stop activation and status.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from aria.api.deps import get_killswitch, get_task_runner, get_notification_service, get_escalation_manager
from aria.notifications.escalation import EscalationManager
from aria.core.killswitch import Killswitch
from aria.tasks.runner import TaskRunner
from aria.notifications.service import NotificationService

router = APIRouter()


class KillswitchActivateRequest(BaseModel):
    reason: str = "Manual activation"


@router.post("/killswitch/activate")
async def activate_killswitch(
    body: KillswitchActivateRequest,
    killswitch: Killswitch = Depends(get_killswitch),
    task_runner: TaskRunner = Depends(get_task_runner),
    notification_service: NotificationService = Depends(get_notification_service),
    escalation_manager: EscalationManager = Depends(get_escalation_manager),
):
    """Activate the emergency killswitch, cancelling all autonomous operations."""
    return await killswitch.activate(
        body.reason,
        task_runner=task_runner,
        notification_service=notification_service,
        escalation_manager=escalation_manager,
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
