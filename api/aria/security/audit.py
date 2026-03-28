"""
ARIA - Audit Logging

Purpose: Persist security and operational audit events.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings


class AuditService:
    """Best-effort audit logging backed by MongoDB."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db

    async def log_event(
        self,
        *,
        category: str,
        action: str,
        status: str,
        actor: Optional[str] = None,
        target: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if not settings.audit_logging_enabled:
            return

        await self.db.audit_logs.insert_one(
            {
                "category": category,
                "action": action,
                "status": status,
                "actor": actor,
                "target": target,
                "metadata": metadata or {},
                "timestamp": datetime.now(timezone.utc),
            }
        )

    async def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.audit_logs.find().sort("timestamp", -1).to_list(length=limit)
        return [self._serialize(row) for row in rows]

    async def summary(self, hours: int = 24) -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        pipeline = [
            {"$match": {"timestamp": {"$gte": since}}},
            {"$group": {"_id": {"category": "$category", "status": "$status"}, "count": {"$sum": 1}}},
        ]
        rows = await self.db.audit_logs.aggregate(pipeline).to_list(length=200)
        return {
            "hours": hours,
            "events": rows,
            "enabled": settings.audit_logging_enabled,
        }

    def _serialize(self, row: dict[str, Any]) -> dict[str, Any]:
        doc = dict(row)
        if "_id" in doc:
            doc["_id"] = str(doc["_id"])
        return doc
