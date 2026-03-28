"""
ARIA - Telegram Handler

Purpose: Process incoming Telegram messages through the orchestrator,
with allowlist enforcement and conversation mapping.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from bson import ObjectId

from aria.config import settings
from aria.telegram.bot import TelegramBot

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorDatabase
    from aria.core.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class TelegramHandler:
    """Handle incoming Telegram messages: allowlist, conversation mapping, orchestrator routing."""

    def __init__(self, bot: TelegramBot):
        self.bot = bot
        self._allowed_users = set(settings.telegram_allowed_users)
        self._poll_task: Optional[asyncio.Task] = None

    def _is_user_allowed(self, username: str) -> bool:
        if settings.telegram_dm_policy == "open":
            return True
        return username in self._allowed_users

    async def _get_or_create_conversation(
        self, chat_id: int, username: str, db: "AsyncIOMotorDatabase"
    ) -> str:
        """Get or create a conversation for a Telegram chat."""
        mapping = await db.telegram_contacts.find_one({"chat_id": chat_id})
        if mapping and mapping.get("conversation_id"):
            return str(mapping["conversation_id"])

        agent = await db.agents.find_one({"is_default": True})
        if not agent:
            agent = await db.agents.find_one({}, sort=[("created_at", 1)])
        if not agent:
            raise RuntimeError("No agent available for Telegram conversation")

        now = datetime.now(timezone.utc)
        conversation = {
            "agent_id": agent["_id"],
            "active_agent_id": None,
            "title": f"Telegram @{username}" if username else f"Telegram {chat_id}",
            "summary": None,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "llm_config": {
                "backend": agent["llm"]["backend"],
                "model": agent["llm"]["model"],
                "temperature": agent["llm"]["temperature"],
            },
            "messages": [],
            "tags": ["telegram"],
            "pinned": False,
            "stats": {"message_count": 0, "total_tokens": 0, "tool_calls": 0},
        }
        result = await db.conversations.insert_one(conversation)
        conversation_id = result.inserted_id

        await db.telegram_contacts.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "chat_id": chat_id,
                    "username": username,
                    "conversation_id": conversation_id,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return str(conversation_id)

    async def handle_update(
        self,
        update: dict,
        db: "AsyncIOMotorDatabase",
        orchestrator: "Orchestrator",
    ) -> None:
        """Process a single Telegram update."""
        message = update.get("message", {})
        text = message.get("text", "")
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        from_user = message.get("from", {})
        username = from_user.get("username", "")

        if not chat_id or not text.strip():
            return

        # Allowlist check
        if not self._is_user_allowed(username):
            logger.info("Telegram message from unauthorized user: %s", username)
            return

        try:
            conversation_id = await self._get_or_create_conversation(chat_id, username, db)

            # Process through orchestrator
            content_parts = []
            async for chunk in orchestrator.process_message(
                conversation_id, text.strip(), stream=False, background_tasks=None
            ):
                if chunk.type == "text" and chunk.content:
                    content_parts.append(chunk.content)
                elif chunk.type == "error":
                    raise RuntimeError(chunk.error or "Processing failed")

            assistant_content = "".join(content_parts).strip()
            if assistant_content:
                # Telegram has a 4096 char limit per message
                for i in range(0, len(assistant_content), 4000):
                    await self.bot.send_message(chat_id, assistant_content[i:i + 4000])

        except Exception as exc:
            logger.error("Telegram handler error for chat %s: %s", chat_id, exc, exc_info=True)

    async def start_polling(
        self,
        db: "AsyncIOMotorDatabase",
        orchestrator: "Orchestrator",
    ) -> None:
        """Start the long-polling loop."""
        if self._poll_task is not None and not self._poll_task.done():
            return

        async def poll_loop():
            while True:
                try:
                    updates = await self.bot.get_updates(timeout=settings.telegram_poll_interval_seconds)
                    for update in updates:
                        await self.handle_update(update, db, orchestrator)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Telegram poll error: %s", exc, exc_info=True)
                    await asyncio.sleep(5)

        self._poll_task = asyncio.create_task(poll_loop())
        logger.info("Telegram polling started")

    async def stop_polling(self) -> None:
        """Stop the polling loop."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        await self.bot.close()

    def status(self) -> dict:
        return {
            "enabled": settings.telegram_enabled,
            "polling": self._poll_task is not None and not self._poll_task.done(),
            "dm_policy": settings.telegram_dm_policy,
            "allowed_users": sorted(self._allowed_users),
        }
