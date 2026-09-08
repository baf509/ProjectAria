"""Operator amendments retain every attempt, usage counter and acceptance gate."""
import copy

import pytest

from aria.ralph.git import OwnershipError
from tests.test_ralph import fixture, create, finish, FakeWorker


async def test_exhausted_run_can_use_explicit_extension_without_resetting_usage(fixture):
    service, _, _ = fixture
    service.worker = FakeWorker([1, 2])
    run = await finish(service, (await create(service, limits={"attempts": 1}))["_id"])
    assert run["state"] == "budget_exhausted"
    run = await service.store.get(run["_id"])
    usage, attempts, started, revision = copy.deepcopy(run["usage"]), copy.deepcopy(run["attempts"]), run["started_at"], run["accepted_revision"]
    changed = await service.extend_limits(run["_id"], {**run["limits"], "attempts": 2}, run["version"], "Repair the retained answer.")
    assert changed["state"] == "paused"
    assert changed["usage"] == usage and changed["attempts"] == attempts
    assert changed["started_at"] == started and changed["accepted_revision"] == revision
    assert changed["deadline"] == started.timestamp() + run["limits"]["run_seconds"]
    assert changed["events"][-1]["kind"] == "limits_extended"
    assert changed["tasks"][0]["handoff"] == "Repair the retained answer."
    assert (await service.store.get(run["_id"]))["finished_at"] is None
    result = await finish(service, run["_id"], "resume")
    assert result["state"] == "ready_for_review"
    assert result["usage"]["attempts"] == 2 and result["usage"]["reported_tokens"] == 20
    assert len({c["session_id"] for c in service.worker.calls}) == 2


async def test_limits_cannot_remove_finite_token_cap_or_reduce_other_limits(fixture):
    service, _, _ = fixture
    run = await create(service, limits={"tokens": 200})
    for limits in ({**run["limits"], "tokens": None}, {**run["limits"], "turns": 1}):
        with pytest.raises(ValueError):
            await service.extend_limits(run["_id"], limits, run["version"])
    assert (await service.store.get(run["_id"]))["version"] == run["version"]


async def test_amendment_cannot_accept_stale_versions_or_advance_cancelled_work(fixture):
    service, _, _ = fixture
    run = await create(service)
    limits = {**run["limits"], "turns": 31}
    with pytest.raises(OwnershipError):
        await service.extend_limits(run["_id"], limits, run["version"] - 1)
    await service.control(run["_id"], "cancel")
    cancelled = await service.store.get(run["_id"])
    with pytest.raises(OwnershipError):
        await service.extend_limits(run["_id"], limits, cancelled["version"])


async def test_live_owner_prevents_amending_limits(fixture):
    service, _, _ = fixture
    run = await create(service)
    await service.store.runs.update_one({"_id": run["_id"]}, {"$set": {"owner": "live-owner"}})
    with pytest.raises(OwnershipError):
        await service.extend_limits(run["_id"], {**run["limits"], "turns": 31}, run["version"])


async def test_cancellation_racing_amendment_wins(fixture, monkeypatch):
    service, _, _ = fixture
    run = await create(service)
    save = service.store.save

    async def cancel_before_save(amended, **kwargs):
        await service.control(run["_id"], "cancel")
        await save(amended, **kwargs)

    monkeypatch.setattr(service.store, "save", cancel_before_save)
    with pytest.raises(OwnershipError):
        await service.extend_limits(run["_id"], {**run["limits"], "turns": 31}, run["version"])
    current = await service.store.get(run["_id"])
    assert current["state"] == "cancelled" and current["limits"] == run["limits"]


async def test_limit_amendment_requires_admin_and_current_version(fixture, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from aria.api.routes.ralph import router, get_ralph
    from aria.config import settings

    service, _, _ = fixture
    run = await create(service)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_ralph] = lambda: service
    monkeypatch.setattr(settings, "admin_key", "operator-only-test-key")
    body = {"expected_version": run["version"], "limits": {**run["limits"], "turns": 31}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        url = f"/ralph/runs/{run['_id']}/limits"
        assert (await client.put(url, json=body)).status_code == 403
        client.headers["X-Admin-Key"] = settings.admin_key
        assert (await client.put(url, json=body)).status_code == 200
        assert (await client.put(url, json=body)).status_code == 409
