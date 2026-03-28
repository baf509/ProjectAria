"""
ARIA - Signal Service

Purpose: Manage Signal REST client lifecycle, configuration, and API-facing state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import mimetypes
import os
from typing import Optional, TYPE_CHECKING
import logging

import httpx
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.signal.client import SignalClient

if TYPE_CHECKING:
    from aria.core.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class SignalService:
    """Coordinator for Signal status, sending, and allowlist management."""

    def __init__(self):
        self._enabled = settings.signal_enabled
        self._allowed_senders = set(settings.signal_allowed_senders)
        self._client: Optional[SignalClient] = None
        self._started_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._last_message_at: Optional[datetime] = None
        self._poll_task: Optional[asyncio.Task] = None

    @property
    def is_started(self) -> bool:
        return self._client is not None

    async def start(self) -> dict:
        if self._client is None:
            self._client = SignalClient(
                base_url=settings.signal_rest_url,
                account=settings.signal_account,
            )
            self._started_at = datetime.now(timezone.utc)
            self._last_error = None

        health = await self._check_health()
        return {
            "started": True,
            "healthy": health,
        }

    async def stop(self) -> dict:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        if self._client is not None:
            await self._client.close()
            self._client = None
        return {"started": False}

    async def _check_health(self) -> bool:
        if self._client is None:
            return False

        try:
            await self._client.health()
            self._last_error = None
            return True
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Signal health check failed: %s", exc)
            return False

    async def status(self) -> dict:
        healthy = await self._check_health() if self._client else False
        return {
            "enabled": self._enabled,
            "started": self.is_started,
            "healthy": healthy,
            "account": settings.signal_account,
            "dm_policy": settings.signal_dm_policy,
            "allowed_senders": sorted(self._allowed_senders),
            "started_at": self._started_at,
            "last_message_at": self._last_message_at,
            "last_error": self._last_error,
            "polling": self._poll_task is not None and not self._poll_task.done(),
        }

    async def add_allowed_sender(self, sender: str) -> dict:
        normalized = sender.strip()
        if not normalized:
            raise ValueError("Sender must not be empty")
        self._allowed_senders.add(normalized)
        return {"allowed_senders": sorted(self._allowed_senders)}

    def _is_sender_allowed(self, sender: str) -> bool:
        if settings.signal_dm_policy == "open":
            return True
        return sender in self._allowed_senders

    async def send(self, recipient: str, message: str) -> dict:
        if not self.is_started:
            await self.start()

        if settings.signal_dm_policy == "allowlist" and not self._is_sender_allowed(recipient):
            raise PermissionError(f"Recipient '{recipient}' is not in the allowlist")

        assert self._client is not None
        try:
            response = await self._client.send_message(recipient, message)
            self._last_message_at = datetime.now(timezone.utc)
            self._last_error = None
            return response
        except httpx.HTTPError as exc:
            self._last_error = str(exc)
            raise

    async def _get_or_create_conversation_for_sender(
        self,
        sender: str,
        db: AsyncIOMotorDatabase,
    ) -> str:
        mapping = await db.signal_contacts.find_one({"sender": sender})
        if mapping and mapping.get("conversation_id"):
            return str(mapping["conversation_id"])

        agent = await db.agents.find_one({"is_default": True})
        if not agent:
            agent = await db.agents.find_one({}, sort=[("created_at", 1)])
        if not agent:
            raise RuntimeError("No agent available for Signal conversation")

        now = datetime.now(timezone.utc)
        conversation = {
            "agent_id": agent["_id"],
            "active_agent_id": None,
            "title": f"Signal {sender}",
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
            "tags": ["signal"],
            "pinned": False,
            "stats": {"message_count": 0, "total_tokens": 0, "tool_calls": 0},
        }
        result = await db.conversations.insert_one(conversation)
        conversation_id = result.inserted_id
        await db.signal_contacts.update_one(
            {"sender": sender},
            {
                "$set": {
                    "sender": sender,
                    "conversation_id": conversation_id,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return str(conversation_id)

    async def handle_incoming_text(
        self,
        *,
        sender: str,
        message: str,
        attachments: Optional[list[dict]] = None,
        db: AsyncIOMotorDatabase,
        orchestrator: Orchestrator,
    ) -> dict:
        normalized_sender = sender.strip()
        if not normalized_sender:
            raise ValueError("Sender must not be empty")
        if not message.strip() and not attachments:
            raise ValueError("Message must not be empty")
        if settings.signal_dm_policy == "allowlist" and not self._is_sender_allowed(normalized_sender):
            raise PermissionError(f"Sender '{normalized_sender}' is not in the allowlist")

        attachment_text = await self._render_attachment_context(attachments or [])
        full_message = message.strip()
        if attachment_text:
            full_message = f"{full_message}\n\n{attachment_text}".strip()

        conversation_id = await self._get_or_create_conversation_for_sender(normalized_sender, db)

        content_parts: list[str] = []
        tool_calls: list[dict] = []
        usage: dict = {}
        async for chunk in orchestrator.process_message(
            conversation_id,
            full_message,
            stream=False,
            background_tasks=None,
        ):
            if chunk.type == "text" and chunk.content:
                content_parts.append(chunk.content)
            elif chunk.type == "tool_call" and chunk.tool_call:
                tool_calls.append(chunk.to_dict()["tool_call"])
            elif chunk.type == "done":
                usage = chunk.usage or {}
            elif chunk.type == "error":
                raise RuntimeError(chunk.error or "Signal message processing failed")

        assistant_content = "".join(content_parts).strip()
        if assistant_content:
            try:
                await self.send(normalized_sender, assistant_content)
            except Exception as exc:
                logger.warning("Failed to send Signal reply: %s", exc, exc_info=True)
        self._last_message_at = datetime.now(timezone.utc)
        return {
            "sender": normalized_sender,
            "conversation_id": conversation_id,
            "response": assistant_content,
            "tool_calls": tool_calls,
            "usage": usage,
        }

    async def poll_once(
        self,
        *,
        db: AsyncIOMotorDatabase,
        orchestrator: Orchestrator,
    ) -> dict:
        if not self.is_started:
            await self.start()
        assert self._client is not None

        envelopes = await self._client.receive_messages()
        processed = 0
        errors = []
        for envelope in envelopes:
            try:
                env = envelope.get("envelope", {})
                data_message = env.get("dataMessage", envelope.get("dataMessage", {}))
                # Skip non-message envelopes (typing indicators, receipts, etc.)
                if not data_message:
                    continue
                sender = env.get("sourceNumber") or env.get("sourceUuid") or env.get("source") or envelope.get("source")
                body = data_message.get("message", "")
                attachments = data_message.get("attachments", [])
                if sender and (body or attachments):
                    await self.handle_incoming_text(
                        sender=sender,
                        message=body,
                        attachments=attachments,
                        db=db,
                        orchestrator=orchestrator,
                    )
                    processed += 1
            except Exception as exc:
                errors.append(str(exc))
                logger.warning("Failed to process Signal envelope: %s", exc, exc_info=True)
        return {"processed": processed, "errors": errors}

    async def start_polling(
        self,
        *,
        db: AsyncIOMotorDatabase,
        orchestrator: Orchestrator,
        interval_seconds: Optional[int] = None,
    ) -> dict:
        if self._poll_task is not None and not self._poll_task.done():
            return {"started": True, "polling": True}

        interval = interval_seconds or settings.signal_poll_interval_seconds

        async def poll_loop():
            while True:
                try:
                    await self.poll_once(db=db, orchestrator=orchestrator)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = str(exc)
                    logger.warning("Signal poll loop error: %s", exc, exc_info=True)
                await asyncio.sleep(interval)

        self._poll_task = asyncio.create_task(poll_loop())
        return {"started": True, "polling": True, "interval_seconds": interval}

    async def _render_attachment_context(self, attachments: list[dict]) -> str:
        rendered = []
        for attachment in attachments:
            path = attachment.get("storedFilename") or attachment.get("path") or attachment.get("filename")
            content_type = attachment.get("contentType") or mimetypes.guess_type(path or "")[0] or "application/octet-stream"
            if not path:
                continue
            full_path = path
            if not os.path.isabs(full_path):
                full_path = os.path.join(os.path.expanduser(settings.signal_attachment_dir), os.path.basename(path))

            line = f"Attachment received: {os.path.basename(full_path)} ({content_type})"
            if content_type.startswith("audio/") and os.path.exists(full_path):
                transcript = await self._transcribe_audio_attachment(full_path, content_type)
                if transcript:
                    line += f"\nTranscript: {transcript}"
            rendered.append(line)
        return "\n\n".join(rendered)

    async def _transcribe_audio_attachment(self, path: str, content_type: str) -> str:
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(path, "rb") as handle:
                response = await client.post(
                    f"{settings.stt_url}/stt/transcribe",
                    files={"file": (os.path.basename(path), handle.read(), content_type)},
                )
        if response.status_code != 200:
            logger.warning("Signal audio transcription failed: %s", response.text)
            return ""
        try:
            payload = response.json()
        except Exception:
            logger.warning("Signal audio transcription returned non-JSON response")
            return ""
        return payload.get("text", "")

    async def shutdown(self) -> None:
        await self.stop()
