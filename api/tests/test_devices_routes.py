"""Route tests for device registration (mobile push)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


class FakeDevicesCollection:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    async def update_one(self, filter, update, upsert=False):
        token = filter["token"]
        existing = self.docs.get(token, {})
        if "$set" in update:
            existing.update(update["$set"])
        if "$setOnInsert" in update and token not in self.docs:
            existing.update(update["$setOnInsert"])
        self.docs[token] = existing

    async def delete_one(self, filter):
        token = filter["token"]
        result = MagicMock()
        if token in self.docs:
            del self.docs[token]
            result.deleted_count = 1
        else:
            result.deleted_count = 0
        return result

    def find(self, _filter):
        docs = list(self.docs.values())

        class _Cursor:
            def __init__(self, docs):
                self._docs = docs

            def __aiter__(self):
                return self._aiter()

            async def _aiter(self):
                for d in self._docs:
                    yield d

        return _Cursor(docs)


class FakeDb:
    def __init__(self):
        self.devices = FakeDevicesCollection()


@pytest.fixture
async def client():
    from aria.main import app
    from aria.api import deps

    db = FakeDb()
    app.dependency_overrides[deps.get_db] = lambda: db

    rl = MagicMock()
    rl.check = MagicMock(return_value=(True, 100))
    with (
        patch("aria.main.settings") as mock_settings,
        patch("aria.main.get_rate_limiter", return_value=rl),
    ):
        mock_settings.api_auth_enabled = False
        mock_settings.cors_origins = ["*"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            ac.fake_db = db  # type: ignore[attr-defined]
            yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register_device_ok(client):
    resp = await client.post(
        "/api/v1/devices",
        json={
            "token": "deadbeef" * 8,
            "platform": "ios",
            "device_name": "Ben's iPhone",
            "app_version": "0.1.0",
        },
    )
    assert resp.status_code == 204
    assert "deadbeef" * 8 in client.fake_db.devices.docs
    doc = client.fake_db.devices.docs["deadbeef" * 8]
    assert doc["platform"] == "ios"
    assert doc["device_name"] == "Ben's iPhone"


@pytest.mark.asyncio
async def test_register_device_rejects_short_token(client):
    resp = await client.post("/api/v1/devices", json={"token": "abc"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_device_upserts(client):
    token = "a" * 64
    await client.post("/api/v1/devices", json={"token": token, "platform": "ios"})
    await client.post("/api/v1/devices", json={"token": token, "platform": "ios", "device_name": "updated"})
    assert client.fake_db.devices.docs[token]["device_name"] == "updated"


@pytest.mark.asyncio
async def test_unregister_device(client):
    token = "b" * 64
    await client.post("/api/v1/devices", json={"token": token, "platform": "ios"})
    resp = await client.delete(f"/api/v1/devices/{token}")
    assert resp.status_code == 204
    assert token not in client.fake_db.devices.docs


@pytest.mark.asyncio
async def test_unregister_missing_device(client):
    resp = await client.delete("/api/v1/devices/not-a-token")
    assert resp.status_code == 404
