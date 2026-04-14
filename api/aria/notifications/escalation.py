"""
ARIA - Escalation Protocol with Severity Routing

Purpose: Route notifications by severity level. Critical issues wake the
user immediately via all channels. Medium issues log quietly.

Inspired by Gas Town's tiered escalation (P0-P2) with severity-based
routing through Agent -> System -> User.

Severity levels:
- CRITICAL (P0): System-threatening, immediate notification via all channels
- HIGH (P1):     Important blocker, notify via primary channel
- MEDIUM (P2):   Standard issue, log + notify if idle
- LOW (P3):      Informational, log only

Escalation chain:
1. Agent detects issue, creates escalation
2. System attempts auto-resolution (retry, fallback, etc.)
3. If unresolved after threshold, escalates to user notification
4. Stale escalations are auto-re-escalated with increased severity
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


class Severity(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3


# Default routing: which notification channels to use per severity
DEFAULT_ROUTES = {
    Severity.LOW: ["log"],
    Severity.MEDIUM: ["log", "db"],
    Severity.HIGH: ["log", "db", "notify"],
    Severity.CRITICAL: ["log", "db", "notify", "notify_all"],
}

# How long before an unresolved escalation gets bumped up
DEFAULT_STALE_THRESHOLDS = {
    Severity.LOW: timedelta(hours=24),
    Severity.MEDIUM: timedelta(hours=4),
    Severity.HIGH: timedelta(hours=1),
    Severity.CRITICAL: timedelta(minutes=15),
}


class Escalation:
    """A tracked escalation event."""

    def __init__(
        self,
        source: str,
        severity: Severity,
        description: str,
        escalation_id: Optional[str] = None,
        status: str = "open",
        resolution: Optional[str] = None,
        created_at: Optional[datetime] = None,
        resolved_at: Optional[datetime] = None,
        re_escalation_count: int = 0,
        metadata: Optional[dict] = None,
    ):
        from uuid import uuid4
        self.escalation_id = escalation_id or str(uuid4())
        self.source = source
        self.severity = severity
        self.description = description
        self.status = status
        self.resolution = resolution
        self.created_at = created_at or datetime.now(timezone.utc)
        self.resolved_at = resolved_at
        self.re_escalation_count = re_escalation_count
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        return {
            "_id": self.escalation_id,
            "source": self.source,
            "severity": self.severity.value,
            "severity_name": self.severity.name,
            "description": self.description,
            "status": self.status,
            "resolution": self.resolution,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "re_escalation_count": self.re_escalation_count,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Escalation:
        return cls(
            escalation_id=data.get("_id"),
            source=data["source"],
            severity=Severity(data["severity"]),
            description=data["description"],
            status=data.get("status", "open"),
            resolution=data.get("resolution"),
            created_at=data.get("created_at"),
            resolved_at=data.get("resolved_at"),
            re_escalation_count=data.get("re_escalation_count", 0),
            metadata=data.get("metadata", {}),
        )


class EscalationManager:
    """Manage escalations with severity-based routing."""

    def __init__(self, db: AsyncIOMotorDatabase, notification_service=None):
        self.db = db
        self.notification_service = notification_service
        self.routes = dict(DEFAULT_ROUTES)
        self.stale_thresholds = dict(DEFAULT_STALE_THRESHOLDS)
        self.max_re_escalations = 2

    async def escalate(
        self,
        source: str,
        severity: Severity,
        description: str,
        metadata: Optional[dict] = None,
    ) -> Escalation:
        """Create a new escalation and route it based on severity."""
        escalation = Escalation(
            source=source,
            severity=severity,
            description=description,
            metadata=metadata,
        )

        await self.db.escalations.insert_one(escalation.to_dict())
        logger.warning(
            "Escalation [%s] %s: %s",
            severity.name, source, description,
        )

        await self._route(escalation)
        return escalation

    async def resolve(
        self,
        escalation_id: str,
        resolution: str,
    ) -> Optional[Escalation]:
        """Resolve an escalation."""
        now = datetime.now(timezone.utc)
        result = await self.db.escalations.find_one_and_update(
            {"_id": escalation_id, "status": "open"},
            {"$set": {
                "status": "resolved",
                "resolution": resolution,
                "resolved_at": now,
            }},
            return_document=True,
        )
        if result:
            logger.info("Escalation %s resolved: %s", escalation_id, resolution)
            return Escalation.from_dict(result)
        return None

    async def get_open(
        self,
        severity: Optional[Severity] = None,
        limit: int = 50,
    ) -> list[Escalation]:
        """Get open escalations, optionally filtered by severity."""
        query: dict = {"status": "open"}
        if severity is not None:
            query["severity"] = {"$gte": severity.value}
        docs = await self.db.escalations.find(query).sort(
            [("severity", -1), ("created_at", 1)]
        ).to_list(length=limit)
        return [Escalation.from_dict(d) for d in docs]

    async def check_stale(self) -> list[Escalation]:
        """Find and re-escalate stale open escalations.

        Called periodically (e.g., from the watchdog or heartbeat).
        Returns list of re-escalated items.
        """
        now = datetime.now(timezone.utc)
        re_escalated = []

        for severity in (Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL):
            threshold = self.stale_thresholds.get(severity)
            if not threshold:
                continue

            cutoff = now - threshold
            stale = await self.db.escalations.find({
                "status": "open",
                "severity": severity.value,
                "created_at": {"$lt": cutoff},
                "re_escalation_count": {"$lt": self.max_re_escalations},
            }).to_list(length=20)

            for doc in stale:
                esc = Escalation.from_dict(doc)
                new_severity = Severity(min(Severity.CRITICAL.value, severity.value + 1))
                await self.db.escalations.update_one(
                    {"_id": esc.escalation_id},
                    {"$set": {
                        "severity": new_severity.value,
                        "severity_name": new_severity.name,
                    }, "$inc": {"re_escalation_count": 1}},
                )
                esc.severity = new_severity
                esc.re_escalation_count += 1
                await self._route(esc)
                re_escalated.append(esc)
                logger.warning(
                    "Re-escalated %s from %s to %s (stale for %s)",
                    esc.escalation_id, severity.name, new_severity.name, threshold,
                )

        return re_escalated

    async def _route(self, escalation: Escalation) -> None:
        """Route an escalation through the configured channels."""
        channels = self.routes.get(escalation.severity, ["log"])

        for channel in channels:
            if channel == "log":
                # Already logged in escalate()
                pass
            elif channel == "db":
                # Already persisted in escalate()
                pass
            elif channel == "notify" and self.notification_service:
                prefix = {
                    Severity.CRITICAL: "CRITICAL",
                    Severity.HIGH: "URGENT",
                    Severity.MEDIUM: "Notice",
                    Severity.LOW: "Info",
                }.get(escalation.severity, "Notice")

                try:
                    await self.notification_service.notify(
                        source=escalation.source,
                        event_type=f"escalation:{escalation.severity.name.lower()}",
                        detail=f"[{prefix}] {escalation.description}",
                        cooldown_seconds=30 if escalation.severity >= Severity.HIGH else 300,
                    )
                except Exception as e:
                    logger.error("Notification delivery failed: %s", e)
            elif channel == "notify_all" and self.notification_service:
                # For CRITICAL: notify all configured channels with no cooldown
                try:
                    await self.notification_service.notify(
                        source=escalation.source,
                        event_type="critical_escalation",
                        detail=f"CRITICAL: {escalation.description}",
                        cooldown_seconds=0,
                    )
                except Exception as e:
                    logger.error("Critical notification delivery failed: %s", e)

    async def status(self) -> dict:
        """Get escalation system status."""
        pipeline = [
            {"$match": {"status": "open"}},
            {"$group": {"_id": "$severity", "count": {"$sum": 1}}},
        ]
        counts = {}
        async for doc in self.db.escalations.aggregate(pipeline):
            sev = Severity(doc["_id"])
            counts[sev.name] = doc["count"]

        return {
            "open_escalations": counts,
            "total_open": sum(counts.values()),
            "max_re_escalations": self.max_re_escalations,
        }
