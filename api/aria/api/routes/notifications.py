"""
ARIA - Notification Routes

Purpose: Send and inspect notifications.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aria.api.deps import get_notification_service
from aria.notifications.service import NotificationService

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationRequest(BaseModel):
    source: str
    event_type: str
    detail: str
    recipient: str | None = None
    cooldown_seconds: int = 60


@router.post("/send")
async def send_notification(
    body: NotificationRequest,
    service: NotificationService = Depends(get_notification_service),
):
    try:
        return await service.notify(
            source=body.source,
            event_type=body.event_type,
            detail=body.detail,
            recipient=body.recipient,
            cooldown_seconds=body.cooldown_seconds,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to send notification: {exc}")


@router.get("/status")
async def notification_status(
    service: NotificationService = Depends(get_notification_service),
):
    return service.status()
