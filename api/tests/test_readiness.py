"""Tests for the dependency-aware startup readiness contract."""

from fastapi import Response
import pytest

from aria.api.routes.health import liveness_check, readiness_check
from aria.core import readiness


@pytest.fixture(autouse=True)
def fresh_readiness_state():
    readiness.reset()
    yield
    readiness.reset()


@pytest.mark.asyncio
async def test_readiness_is_503_until_boot_is_complete():
    readiness.mark_phase("database", "MongoDB connection")
    response = Response()

    result = await readiness_check(response)

    assert response.status_code == 503
    assert result["ready"] is False
    assert result["phase"] == "database"
    assert result["blocked_on"] == "MongoDB connection"


@pytest.mark.asyncio
async def test_readiness_becomes_200_with_stable_boot_metadata():
    boot_id = readiness.snapshot()["boot_id"]
    readiness.mark_ready()
    response = Response()

    result = await readiness_check(response)

    assert response.status_code == 200
    assert result["ready"] is True
    assert result["phase"] == "ready"
    assert result["boot_id"] == boot_id
    assert result["ready_at"] is not None


@pytest.mark.asyncio
async def test_liveness_has_no_dependency_probe():
    result = await liveness_check()
    assert result["live"] is True
