"""
ARIA - Signal REST Client

Purpose: Thin async client for signal-cli-rest-api.
"""

from __future__ import annotations

import httpx


class SignalClient:
    """HTTP client wrapper for signal-cli REST API."""

    def __init__(self, base_url: str, account: str = ""):
        self.base_url = base_url.rstrip("/")
        self.account = account
        self.client = httpx.AsyncClient(timeout=30.0)

    async def send_message(self, recipient: str, message: str) -> dict:
        payload = {
            "message": message,
            "number": self.account,
            "recipients": [recipient],
        }
        response = await self.client.post(f"{self.base_url}/v2/send", json=payload)
        response.raise_for_status()
        return response.json()

    async def health(self) -> dict:
        response = await self.client.get(f"{self.base_url}/v1/health")
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {"status": "ok"}
        return response.json()

    async def receive_messages(self) -> list[dict]:
        endpoint = f"{self.base_url}/v1/receive/{self.account}" if self.account else f"{self.base_url}/v1/receive"
        response = await self.client.get(endpoint, params={"timeout": 1, "ignore_attachments": "false"})
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return payload
        return payload.get("messages", [])

    async def close(self) -> None:
        await self.client.aclose()
