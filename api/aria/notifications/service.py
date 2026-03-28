"""
ARIA - Notification Service

Purpose: Cooldown-aware notifications with Signal delivery.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from aria.config import settings
from aria.signal.service import SignalService

import logging

logger = logging.getLogger(__name__)


class NotificationService:
    """Send notifications through configured channels with cooldowns."""

    def __init__(self, signal_service: SignalService):
        self.signal_service = signal_service
        self._telegram_bot = None  # Set via set_telegram_bot()
        self._cooldowns: dict[tuple[str, str], datetime] = {}

    def set_telegram_bot(self, bot) -> None:
        """Set the Telegram bot for secondary delivery."""
        self._telegram_bot = bot

    def _resolve_recipient(self, recipient: Optional[str]) -> str:
        if recipient:
            return recipient
        if settings.signal_allowed_senders:
            return settings.signal_allowed_senders[0]
        raise RuntimeError("No Signal recipient configured for notifications")

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
        recipient: Optional[str] = None,
        cooldown_seconds: int = 60,
    ) -> dict:
        if not self._can_send(source, event_type, cooldown_seconds):
            return {
                "sent": False,
                "reason": "cooldown",
            }

        try:
            target = self._resolve_recipient(recipient)
        except RuntimeError as exc:
            return {
                "sent": False,
                "reason": "unconfigured",
                "detail": str(exc),
            }

        message = f"[{source}] {event_type.upper()}: {detail}"
        try:
            response = await self.signal_service.send(target, message)
        except Exception as exc:
            return {
                "sent": False,
                "reason": "send_failed",
                "detail": str(exc),
            }
        self._mark_sent(source, event_type)

        # Also try Telegram as secondary channel
        if self._telegram_bot and settings.telegram_enabled:
            try:
                for user in settings.telegram_allowed_users[:1]:
                    # We need a chat_id, not username, for Telegram delivery.
                    # Skip if not numeric (can't send without chat_id).
                    if user.isdigit():
                        await self._telegram_bot.send_message(int(user), message)
            except Exception as exc:
                logger.warning("Telegram notification delivery failed: %s", exc)

        return {
            "sent": True,
            "recipient": target,
            "message": message,
            "response": response,
        }

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
