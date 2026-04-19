"""
ARIA - Device registration routes (mobile push)

Purpose: Allow iOS clients to register an APNs device token so that the
idle notifier can fan out alerts to the phone. Tokens are stored in the
`devices` Mongo collection and are deleted on logout or token refresh.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from aria.api.deps import get_db


router = APIRouter()


class DeviceRegistration(BaseModel):
    token: str = Field(..., min_length=8, max_length=512)
    platform: str = "ios"
    device_name: Optional[str] = None
    app_version: Optional[str] = None


@router.post("/devices", status_code=204)
async def register_device(
    body: DeviceRegistration,
    db=Depends(get_db),
):
    """Upsert a device token. Idempotent on (token)."""
    now = datetime.now(timezone.utc)
    await db.devices.update_one(
        {"token": body.token},
        {
            "$set": {
                "platform": body.platform,
                "device_name": body.device_name,
                "app_version": body.app_version,
                "last_seen_at": now,
            },
            "$setOnInsert": {"token": body.token, "created_at": now},
        },
        upsert=True,
    )
    return None


@router.delete("/devices/{token}", status_code=204)
async def unregister_device(token: str, db=Depends(get_db)):
    result = await db.devices.delete_one({"token": token})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    return None


@router.get("/devices")
async def list_devices(db=Depends(get_db)):
    """Debug helper — list registered devices (no secrets)."""
    out = []
    async for doc in db.devices.find({}):
        doc.pop("_id", None)
        doc["token"] = doc.get("token", "")[:8] + "…"
        out.append(doc)
    return {"devices": out}
