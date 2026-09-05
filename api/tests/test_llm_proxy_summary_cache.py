"""One fleet observation per concurrent gateway burst; never stale resurrection."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.api.routes import llm_proxy as proxy


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(proxy, '_summary_cache', None)
    monkeypatch.setattr(proxy, '_summary_lock', asyncio.Lock())
    monkeypatch.setattr(proxy, '_summary_generation', 0)


@pytest.mark.asyncio
async def test_concurrent_clients_share_one_probe_and_completion_timestamp(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(proxy.time, 'monotonic', lambda: clock[0])
    entered, release = asyncio.Event(), asyncio.Event()
    rows = [{'slug': 'resident', 'state': 'running'}]

    async def probe(_db):
        entered.set()
        await release.wait()
        clock[0] = 105.0
        return rows

    manager = MagicMock(running_summary=AsyncMock(side_effect=probe))
    tasks = [asyncio.create_task(proxy._running_summary_cached(manager, None)) for _ in range(12)]
    await entered.wait()
    release.set()
    assert await asyncio.gather(*tasks) == [rows] * 12
    manager.running_summary.assert_awaited_once()
    assert proxy._summary_cache == (105.0, rows)
    clock[0] = 108.1
    await proxy._running_summary_cached(manager, None)
    assert manager.running_summary.await_count == 2


@pytest.mark.asyncio
async def test_invalidation_during_probe_cannot_resurrect_old_cache():
    async def probe(_db):
        proxy._drop_summary_cache()
        return [{'slug': 'old'}]

    manager = MagicMock(running_summary=AsyncMock(side_effect=probe))
    await proxy._running_summary_cached(manager, None)
    assert proxy._summary_cache is None
    await proxy._running_summary_cached(manager, None)
    assert manager.running_summary.await_count == 2


@pytest.mark.asyncio
async def test_failed_probe_releases_lock_and_does_not_cache_failure():
    manager = MagicMock(running_summary=AsyncMock(side_effect=[RuntimeError('probe failed'), []]))
    with pytest.raises(RuntimeError):
        await proxy._running_summary_cached(manager, None)
    assert proxy._summary_cache is None
    assert await proxy._running_summary_cached(manager, None) == []


@pytest.mark.asyncio
async def test_cancelled_probe_does_not_block_next_client():
    entered = asyncio.Event()

    async def probe(_db):
        entered.set()
        await asyncio.Event().wait()

    manager = MagicMock(running_summary=AsyncMock(side_effect=probe))
    task = asyncio.create_task(proxy._running_summary_cached(manager, None))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proxy._summary_cache is None
    manager.running_summary = AsyncMock(return_value=[])
    assert await proxy._running_summary_cached(manager, None) == []


def test_client_reuses_pool_but_retires_idle_sockets_before_backend_expiry(monkeypatch):
    client = MagicMock(is_closed=False)
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(proxy, '_shared_client', None)
    monkeypatch.setattr(proxy.httpx, 'AsyncClient', factory)
    assert proxy._client() is client
    assert proxy._client() is client
    factory.assert_called_once()
    limits = factory.call_args.kwargs['limits']
    assert limits.keepalive_expiry == 2.0
    assert limits.max_connections == 100
    assert limits.max_keepalive_connections == 20
    client.is_closed = True
    proxy._client()
    assert factory.call_count == 2
