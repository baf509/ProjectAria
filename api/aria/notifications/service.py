"""
ARIA - Notification Service

Purpose: Cooldown-aware alerting. ProjectAria does NOT deliver Signal/Telegram
itself — that collided with the single signal-cli daemon owned by the Hermes
agent. Instead `notify()` enqueues alerts into the `alerts` collection; Hermes
pulls them over MCP (list_alerts / ack_alert) and relays them via its own
Signal. The cooldown logic is retained so a sustained outage doesn't flood the
queue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from aria.config import settings
from aria.signal.service import SignalService

import logging

logger = logging.getLogger(__name__)


class NotificationService:
    """Enqueue cooldown-gated alerts for the Hermes agent to relay over MCP."""

    def __init__(self, signal_service: Optional[SignalService] = None):
        # signal_service is retained for constructor compatibility but no longer
        # used for delivery — ProjectAria queues alerts instead of sending them.
        self.signal_service = signal_service
        self._telegram_bot = None  # retained no-op; ProjectAria does not send TG
        self._cooldowns: dict[tuple[str, str], datetime] = {}

    def set_telegram_bot(self, bot) -> None:
        """No-op retained for caller compatibility. ProjectAria no longer
        delivers Telegram directly (alerts go to the MCP queue)."""
        self._telegram_bot = bot

    def _can_send(self, source: str, event_type: str, cooldown_seconds: int) -> bool:
        key = (source, event_type)
        last_sent = self._cooldowns.get(key)
        if last_sent is None:
            return True
        return datetime.now(timezone.utc) - last_sent >= timedelta(seconds=cooldown_seconds)

    def _mark_sent(self, source: str, event_type: str) -> None:
        self._cooldowns[(source, event_type)] = datetime.now(timezone.utc)

    async def notify(
        self,
        *,
        source: str,
        event_type: str,
        detail: str,
        recipient: Optional[str] = None,  # accepted for compat; unused
        cooldown_seconds: int = 60,
    ) -> dict:
        """Enqueue an alert for relay. Returns {queued: bool, ...}. Honors the
        per-(source, event_type) cooldown so repeats within the window are
        dropped (returns queued=False, reason='cooldown')."""
        if not self._can_send(source, event_type, cooldown_seconds):
            return {"queued": False, "reason": "cooldown"}

        message = f"[{source}] {event_type.upper()}: {detail}"
        now = datetime.now(timezone.utc)
        doc = {
            "source": source,
            "event_type": event_type,
            "detail": detail,
            "message": message,
            "acked": False,
            "created_at": now,
            "acked_at": None,
        }
        try:
            from aria.db.mongodb import get_database
            db = await get_database()
            result = await db.alerts.insert_one(doc)
        except Exception as exc:
            logger.warning("alert enqueue failed (%s/%s): %s", source, event_type, exc)
            return {"queued": False, "reason": "enqueue_failed", "detail": str(exc)}

        self._mark_sent(source, event_type)
        return {"queued": True, "alert_id": str(result.inserted_id), "message": message}

    def status(self) -> dict:
        return {
            "tracked_cooldowns": [
                {
                    "source": source,
                    "event_type": event_type,
                    "last_sent_at": sent_at,
                }
                for (source, event_type), sent_at in sorted(self._cooldowns.items())
            ]
        }
