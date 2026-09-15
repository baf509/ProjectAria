"""Downstream departure must cancel queued and accepted upstream requests."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from aria.api.routes import llm_proxy as proxy
from tests.conftest import make_mock_db
from tests.test_llm_proxy_usage import _request


@pytest.mark.asyncio
async def test_completed_response_wins_without_lingering_watcher():
    async def no_disconnect():
        return False

    async def result():
        return 'complete'

    request = SimpleNamespace(is_disconnected=no_disconnect)
    assert await proxy._while_connected(request, result) == 'complete'


@pytest.mark.asyncio
async def test_departed_queue_waiter_never_starts_upstream():
    departed = False

    async def is_disconnected():
        return departed

    admission = proxy._PriorityAdmission(aging_seconds=30)
    # Same primitive used by the gateway, including cancelled-waiter cleanup.
    await admission.acquire(1)
    started = []

    async def queued():
        await admission.acquire(1)
        try:
            started.append(True)
        finally:
            await admission.release()

    request = SimpleNamespace(is_disconnected=is_disconnected)
    task = asyncio.create_task(proxy._while_connected(request, queued))
    await asyncio.sleep(.02)
    departed = True
    with pytest.raises(proxy._ClientDisconnected):
        await task
    await admission.release()
    assert not started


@pytest.mark.asyncio
async def test_disconnect_closes_accepted_tcp_request_without_replay():
    accepted = asyncio.Event()
    closed = asyncio.Event()
    departed = False
    count = 0

    async def serve(reader, writer):
        nonlocal count
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            length = next(int(x.split(b':', 1)[1]) for x in head.split(b'\r\n')
                          if x.lower().startswith(b'content-length:'))
            await reader.readexactly(length)
            count += 1
            accepted.set()
            assert await reader.read() == b''
            closed.set()
        finally:
            writer.close()
            await writer.wait_closed()

    async def is_disconnected():
        return departed

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    async with httpx.AsyncClient(timeout=5) as client:
        async def post():
            port = server.sockets[0].getsockname()[1]
            return await client.post(f'http://127.0.0.1:{port}/test', content=b'test')

        request = SimpleNamespace(is_disconnected=is_disconnected)
        task = asyncio.create_task(proxy._while_connected(request, post))
        try:
            await asyncio.wait_for(accepted.wait(), 2)
            departed = True
            with pytest.raises(proxy._ClientDisconnected):
                await task
            await asyncio.wait_for(closed.wait(), 2)
            assert count == 1
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize('outer_cancel', [False, True])
async def test_abandoned_request_is_closed_and_accounted_once(monkeypatch, outer_cancel):
    db = make_mock_db()
    route = proxy._Route('Red-Qwen3.8-27B-PARO-INT5', 'http://127.0.0.1:8004/v1', 'explicit', [])
    monkeypatch.setattr(proxy, '_pick_backend', AsyncMock(return_value=route))
    monkeypatch.setattr(proxy, '_backend_model_id_cached', AsyncMock(return_value='test-model'))
    request = _request({'model': route.slug, 'messages': [{'role': 'user', 'content': 'private fixture'}]},
                       caller='runtime-rollout-qualification', session_id='test-session')
    accepted = asyncio.Event()
    closed = asyncio.Event()
    departed = False
    async def disconnected():
        return departed
    monkeypatch.setattr(request, 'is_disconnected', disconnected)
    async def post(*args, **kwargs):
        accepted.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    client = MagicMock(post=AsyncMock(side_effect=post))
    monkeypatch.setattr(proxy, '_client', lambda: client)
    task = asyncio.create_task(proxy._proxy('chat/completions', request, MagicMock(), db))
    await asyncio.wait_for(accepted.wait(), 2)
    if outer_cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    else:
        departed = True
        with pytest.raises(HTTPException) as raised:
            await asyncio.wait_for(task, 2)
        assert raised.value.status_code == 499
    assert closed.is_set()
    client.post.assert_awaited_once()
    db.usage.insert_one.assert_awaited_once()
    doc = db.usage.insert_one.call_args.args[0]
    assert doc['metadata']['status_code'] == 499
    assert doc['session_id'] == 'test-session'
    assert len(doc['trace_id']) == 32
    assert 'private fixture' not in repr(doc)
