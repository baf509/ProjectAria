"""
ARIA - Telegram Bot

Purpose: Thin httpx wrapper around Telegram Bot API (getUpdates/sendMessage).
Long-polling loop, no webhook needed (local-first).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from aria.config import settings

logger = logging.getLogger(__name__)


class TelegramBot:
    """Minimal Telegram Bot API client using httpx."""

    def __init__(self, token: str = ""):
        self._token = token or settings.telegram_bot_token
        self._base_url = f"https://api.telegram.org/bot{self._token}"
        self._client: Optional[httpx.AsyncClient] = None
        self._last_update_id: int = 0

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def get_me(self) -> dict:
        """Test bot token and get bot info."""
        client = await self._ensure_client()
        resp = await client.get(f"{self._base_url}/getMe")
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data["result"]

    async def get_updates(self, timeout: int = 30) -> list[dict]:
        """Long-poll for new messages."""
        client = await self._ensure_client()
        params = {
            "offset": self._last_update_id + 1,
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        try:
            resp = await client.get(
                f"{self._base_url}/getUpdates",
                params=params,
                timeout=timeout + 10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                return []

            updates = data.get("result", [])
            if updates:
                self._last_update_id = updates[-1]["update_id"]
            return updates
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            logger.warning("Telegram getUpdates error: %s", exc)
            return []

    async def send_message(self, chat_id: int, text: str) -> dict:
        """Send a text message."""
        client = await self._ensure_client()
        resp = await client.post(
            f"{self._base_url}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {data}")
        return data["result"]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
