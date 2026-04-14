"""
ARIA - Emergency Stop (Estop) and Rate Limit Watchdog

Purpose: Global emergency stop that freezes all agent activity when API
rate limits or critical errors are detected. Auto-thaws when clear.

Inspired by Gas Town's rate-limit-watchdog plugin.

The estop state is stored in MongoDB so it's visible across all ARIA
processes and persists across restarts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


class EstopState:
    """Represents the current emergency stop state."""

    def __init__(
        self,
        active: bool = False,
        reason: Optional[str] = None,
        triggered_by: Optional[str] = None,
        triggered_at: Optional[datetime] = None,
        auto_thaw: bool = True,
    ):
        self.active = active
        self.reason = reason
        self.triggered_by = triggered_by
        self.triggered_at = triggered_at
        self.auto_thaw = auto_thaw

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "reason": self.reason,
            "triggered_by": self.triggered_by,
            "triggered_at": self.triggered_at,
            "auto_thaw": self.auto_thaw,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EstopState:
        return cls(
            active=data.get("active", False),
            reason=data.get("reason"),
            triggered_by=data.get("triggered_by"),
            triggered_at=data.get("triggered_at"),
            auto_thaw=data.get("auto_thaw", True),
        )


class EstopManager:
    """Automated emergency stop for agent operations.

    Distinct from the manual Killswitch (core/killswitch.py):
    - Killswitch: Human-triggered, cancels tasks, requires manual deactivation.
    - Estop: System-triggered (rate limits), auto-thaws when healthy.

    Both share the same check path: agent tools check estop.is_active(),
    and the killswitch is checked via killswitch.check_or_raise().
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self._cached_state: Optional[EstopState] = None
        self._cache_time: float = 0

    async def get_state(self) -> EstopState:
        """Get the current estop state (cached for 5 seconds)."""
        import time
        now = time.monotonic()
        if self._cached_state and now - self._cache_time < 5:
            return self._cached_state

        doc = await self.db.estop.find_one({"_id": "global"})
        if doc is None:
            self._cached_state = EstopState()
        else:
            self._cached_state = EstopState.from_dict(doc)
        self._cache_time = now
        return self._cached_state

    async def is_active(self) -> bool:
        """Check if emergency stop is currently active."""
        state = await self.get_state()
        return state.active

    async def activate(
        self,
        reason: str,
        triggered_by: str = "system",
        auto_thaw: bool = True,
    ) -> EstopState:
        """Activate the emergency stop."""
        now = datetime.now(timezone.utc)
        state = EstopState(
            active=True,
            reason=reason,
            triggered_by=triggered_by,
            triggered_at=now,
            auto_thaw=auto_thaw,
        )

        await self.db.estop.update_one(
            {"_id": "global"},
            {"$set": state.to_dict()},
            upsert=True,
        )
        self._cached_state = state
        self._cache_time = 0  # invalidate cache

        # Log the event
        await self.db.estop_log.insert_one({
            "action": "activate",
            "reason": reason,
            "triggered_by": triggered_by,
            "auto_thaw": auto_thaw,
            "timestamp": now,
        })

        logger.warning("ESTOP ACTIVATED: %s (by %s)", reason, triggered_by)
        return state

    async def deactivate(self, reason: str = "manual") -> EstopState:
        """Deactivate the emergency stop (thaw)."""
        state = EstopState(active=False)
        await self.db.estop.update_one(
            {"_id": "global"},
            {"$set": state.to_dict()},
            upsert=True,
        )
        self._cached_state = state
        self._cache_time = 0

        await self.db.estop_log.insert_one({
            "action": "deactivate",
            "reason": reason,
            "timestamp": datetime.now(timezone.utc),
        })

        logger.info("ESTOP deactivated: %s", reason)
        return state

    async def get_log(self, limit: int = 20) -> list[dict]:
        """Get recent estop events."""
        cursor = self.db.estop_log.find({}).sort("timestamp", -1).limit(limit)
        return await cursor.to_list(length=limit)


class RateLimitWatchdog:
    """Background watchdog that monitors for API rate limits and triggers estop.

    Checks all configured cloud backends by inspecting circuit breaker
    state. When rate limiting is detected, activates global estop.
    Auto-thaws when the rate limit clears.
    """

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        estop: EstopManager,
        notification_service=None,
    ):
        self.db = db
        self.estop = estop
        self.notification_service = notification_service
        self._task: Optional[asyncio.Task] = None
        self._check_interval = 180  # 3 minutes

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("Rate limit watchdog started (interval=%ds)", self._check_interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("Rate limit watchdog stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await self._check()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Rate limit watchdog error: %s", e)
            await asyncio.sleep(self._check_interval)

    async def _check(self) -> None:
        """Check cloud backends for rate limit status."""
        from aria.llm.manager import llm_manager

        rate_limited_backends = []

        for backend_name in ("anthropic", "openai", "openrouter"):
            available, _ = llm_manager.is_backend_available(backend_name)
            if not available:
                continue

            healthy = await llm_manager.is_backend_healthy(backend_name)
            if not healthy:
                # Circuit breaker is open — likely rate limited
                failures = llm_manager._failure_counts.get(backend_name, 0)
                rate_limited_backends.append(f"{backend_name} ({failures} failures)")

        estop_state = await self.estop.get_state()

        if rate_limited_backends:
            if not estop_state.active:
                reason = f"API rate limit detected: {', '.join(rate_limited_backends)}"
                await self.estop.activate(
                    reason=reason,
                    triggered_by="rate_limit_watchdog",
                    auto_thaw=True,
                )
                if self.notification_service:
                    try:
                        await self.notification_service.notify(
                            source="rate_limit_watchdog",
                            event_type="estop",
                            detail=reason,
                            cooldown_seconds=300,
                        )
                    except Exception:
                        pass
        else:
            # All backends healthy — thaw if estop was triggered by us
            if (
                estop_state.active
                and estop_state.auto_thaw
                and estop_state.triggered_by == "rate_limit_watchdog"
            ):
                await self.estop.deactivate(reason="Rate limits cleared")
                if self.notification_service:
                    try:
                        await self.notification_service.notify(
                            source="rate_limit_watchdog",
                            event_type="thaw",
                            detail="All API backends healthy — agents resumed",
                            cooldown_seconds=300,
                        )
                    except Exception:
                        pass

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "check_interval_seconds": self._check_interval,
        }
