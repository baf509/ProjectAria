"""
ARIA - Alert Queue Routes

ProjectAria does not push notifications itself; its workers (selfcheck,
idle-notifier, weekly report, watchdog) enqueue alerts via NotificationService
into the `alerts` collection. The relay pulls them (list_alerts), delivers them
over the Signal daemon it owns, marks them delivered, and acks them. Mounted
under /api/v1 → /alerts.

Alerts v2 (steward proposal §3.1): rows carry severity/kind/needs_human/
dedup_key/delivered_at/proposal/decision, so the relay can select the few that
actually need Ben (`needs_human=true, undelivered=true`) instead of relaying the
whole queue, and Ben's answer comes back as a typed decision with an id rather
than free text.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from aria.api.deps import get_db

router = APIRouter(prefix="/alerts", tags=["alerts"])

# APPLY/REJECT/STOP are the actions Hermes offers in the Signal reply menu;
# HOLD is what an unanswered urgent item auto-expires to (never APPLY);
# IGNORE means "this raise was unnecessary" and is the false-raise signal.
DECISION_ACTIONS = ("APPLY", "REJECT", "STOP", "HOLD", "IGNORE")


class DecideRequest(BaseModel):
    action: str
    by: str = "ben"
    note: Optional[str] = None


class DeliveredRequest(BaseModel):
    by: Optional[str] = None


class HeartbeatRequest(BaseModel):
    source: str = Field(default="relay")


def _serialize(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc


def _oid(alert_id: str) -> ObjectId:
    try:
        return ObjectId(alert_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid alert id: {alert_id}")


@router.get("")
async def list_alerts(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    unacked_only: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=500),
    needs_human: Optional[bool] = Query(default=None),
    undelivered: Optional[bool] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    project: Optional[str] = Query(default=None, description="project slug or workspace path"),
    source: Optional[str] = Query(default=None),
):
    """List alerts newest-first. By default only un-acked ones (the relay
    queue). The relay wants `needs_human=true&undelivered=true`; the cockpit
    and digest read the rest."""
    query: dict = {}
    if unacked_only:
        query["acked"] = False
    if needs_human is not None:
        # Rows written before Alerts v2 have no needs_human field. "$ne: true"
        # (not "false") keeps them out of the raise-to-Ben lane rather than
        # silently reclassifying history.
        query["needs_human"] = True if needs_human else {"$ne": True}
    if undelivered is not None:
        query["delivered_at"] = None if undelivered else {"$ne": None}
    if severity:
        query["severity"] = severity
    if kind:
        query["kind"] = kind
    if source:
        query["source"] = source
    if project:
        # Accept either form: the cockpit knows slugs, the watchdog attributes
        # by workspace path, and older rows carry only the path.
        query["$or"] = [
            {"project_slug": project},
            {"project_path": project},
            {"project_path": {"$regex": f"/{re.escape(project)}/?$"}},
        ]
    cursor = db.alerts.find(query).sort("created_at", -1).limit(int(limit))
    alerts = [_serialize(doc) async for doc in cursor]
    return {"alerts": alerts, "count": len(alerts)}


@router.post("/{alert_id}/ack")
async def ack_alert(
    alert_id: str,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Mark an alert acknowledged so it is not relayed again."""
    result = await db.alerts.update_one(
        {"_id": _oid(alert_id)},
        {"$set": {"acked": True, "acked_at": datetime.now(timezone.utc)}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail=f"Alert not found: {alert_id}")
    return {"ok": True, "id": alert_id, "acked": True}


@router.post("/{alert_id}/decide")
async def decide_alert(
    alert_id: str,
    body: DecideRequest,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Record Ben's answer to a raise. Acks the alert as a side effect: a
    decided alert must never be relayed again."""
    action = (body.action or "").strip().upper()
    if action not in DECISION_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of {', '.join(DECISION_ACTIONS)}",
        )
    now = datetime.now(timezone.utc)
    update: dict = {
        "decision": {"by": body.by, "at": now, "value": action, "note": body.note},
        "acked": True,
        "acked_at": now,
        # A decided alert stops being a raise: the relay selects on needs_human,
        # so leaving it true would re-deliver something Ben has already answered.
        "needs_human": False,
    }
    if action == "IGNORE":
        # Ben saying "you shouldn't have raised this" is the false-raise metric
        # (proposal §6.3, target ≤3 raises/day) — it has to be a queryable field,
        # not a note.
        update["false_raise"] = True
    doc = await db.alerts.find_one_and_update(
        {"_id": _oid(alert_id)},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail=f"Alert not found: {alert_id}")
    return {"ok": True, "id": alert_id, "decision": action, "alert": _serialize(doc)}


@router.post("/{alert_id}/delivered")
async def mark_delivered(
    alert_id: str,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    body: Optional[DeliveredRequest] = None,
):
    """Called by the relay after a successful Signal send. Distinct from ack:
    delivered means Ben saw it, acked means it is closed. Without this split,
    "queued but never sent" and "sent and handled" look identical — which is how
    three relay outages stayed invisible."""
    now = datetime.now(timezone.utc)
    update = {"delivered_at": now}
    if body is not None and body.by:
        update["delivered_by"] = body.by
    result = await db.alerts.update_one({"_id": _oid(alert_id)}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail=f"Alert not found: {alert_id}")
    return {"ok": True, "id": alert_id, "delivered_at": now}


@router.post("/relay-heartbeat")
async def relay_heartbeat(
    request: Request,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    body: Optional[HeartbeatRequest] = None,
):
    """Record that the outbox relay ran. The relay cron calls this every pass,
    delivered anything or not — "I am alive" and "I had something to send" are
    different facts, and conflating them is why an idle relay looked healthy
    while a dead one looked idle."""
    source = body.source if body is not None else "relay"
    watchdog = getattr(request.app.state, "relay_watchdog", None)
    if watchdog is not None:
        # Through the worker when it exists, so a heartbeat that ends an outage
        # also raises the relay:recovered notice.
        state = await watchdog.record_heartbeat(source)
        return {"ok": True, "source": source, "recovered": bool(state.get("recovered"))}
    from aria.notifications.relay import record_heartbeat

    await record_heartbeat(db, source)
    return {"ok": True, "source": source, "recovered": False}
