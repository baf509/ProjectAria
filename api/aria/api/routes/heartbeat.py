"""
ARIA - Heartbeat API Routes

Purpose: Status and manual control of the heartbeat system.
"""

from fastapi import APIRouter, Depends

from aria.api.deps import get_heartbeat_service
from aria.heartbeat.service import HeartbeatService

router = APIRouter()


@router.get("/heartbeat/status")
async def heartbeat_status(
    service: HeartbeatService = Depends(get_heartbeat_service),
):
    """Get current heartbeat status and configuration."""
    return service.status()


@router.post("/heartbeat/trigger")
async def heartbeat_trigger(
    service: HeartbeatService = Depends(get_heartbeat_service),
):
    """Manually trigger a heartbeat check."""
    return await service.trigger()
