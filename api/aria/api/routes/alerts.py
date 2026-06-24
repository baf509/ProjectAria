"""
ARIA - Alert Queue Routes

ProjectAria does not deliver Signal/Telegram itself; its workers (selfcheck,
idle-notifier, weekly report) enqueue alerts via NotificationService into the
`alerts` collection. The Hermes agent pulls them over MCP (list_alerts), relays
them over its own Signal daemon, and acks each one (ack_alert) so it is not
relayed again. Mounted under /api/v1 → /alerts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.api.deps import get_db

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _serialize(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.get("")
async def list_alerts(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    unacked_only: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=500),
):
    """List alerts newest-first. By default only un-acked ones (the relay
    queue). Set unacked_only=false to include already-relayed alerts."""
    query: dict = {}
    if unacked_only:
        query["acked"] = False
    cursor = db.alerts.find(query).sort("created_at", -1).limit(int(limit))
    alerts = [_serialize(doc) async for doc in cursor]
    return {"alerts": alerts, "count": len(alerts)}


@router.post("/{alert_id}/ack")
async def ack_alert(
    alert_id: str,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Mark an alert acknowledged so it is not relayed again."""
    try:
        oid = ObjectId(alert_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid alert id: {alert_id}")
    result = await db.alerts.update_one(
        {"_id": oid},
        {"$set": {"acked": True, "acked_at": datetime.now(timezone.utc)}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail=f"Alert not found: {alert_id}")
    return {"ok": True, "id": alert_id, "acked": True}
