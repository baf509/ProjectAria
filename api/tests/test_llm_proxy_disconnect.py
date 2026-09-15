"""Downstream departure must cancel queued and accepted upstream requests."""
import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
import uvicorn

from aria.api.routes import llm_proxy as proxy
from tests.conftest import make_mock_db
from tests.test_llm_proxy_usage import _request


@pytest.mark.asyncio
async def test_completed_response_wins_without_lingering_watcher():
    async def no_disconnect():
        await asyncio.Event().wait()

    async def result():
        return 'complete'

    request = SimpleNamespace(receive=no_disconnect)
    assert await proxy._while_connected(request, result) == 'complete'


@pytest.mark.asyncio
async def test_departed_queue_waiter_never_starts_upstream():
    departed = asyncio.Event()

    async def receive():
        await departed.wait()
        return {'type': 'http.disconnect'}

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

    request = SimpleNamespace(receive=receive)
    task = asyncio.create_task(proxy._while_connected(request, queued))
    await asyncio.sleep(.02)
    departed.set()
    with pytest.raises(proxy._ClientDisconnected):
        await task
    await admission.release()
    assert not started


@pytest.mark.asyncio
async def test_disconnect_closes_accepted_tcp_request_without_replay():
    accepted = asyncio.Event()
    closed = asyncio.Event()
    departed = asyncio.Event()
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

    async def receive():
        await departed.wait()
        return {'type': 'http.disconnect'}

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    async with httpx.AsyncClient(timeout=5) as client:
        async def post():
            port = server.sockets[0].getsockname()[1]
            return await client.post(f'http://127.0.0.1:{port}/test', content=b'test')

        request = SimpleNamespace(receive=receive)
        task = asyncio.create_task(proxy._while_connected(request, post))
        try:
            await asyncio.wait_for(accepted.wait(), 2)
            departed.set()
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
    departed = asyncio.Event()
    request = _request({'model': route.slug, 'messages': [{'role': 'user', 'content': 'private fixture'}]},
                       caller='runtime-rollout-qualification', session_id='test-session', disconnected=departed)
    accepted = asyncio.Event()
    closed = asyncio.Event()
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
        departed.set()
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


@pytest.mark.asyncio
async def test_real_http_disconnect_crosses_basehttp_middleware(monkeypatch):
    """A cancelled receive poll misses disconnects behind BaseHTTPMiddleware."""
    accepted = asyncio.Event()
    closed = asyncio.Event()
    db = make_mock_db()

    async def upstream(reader, writer):
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            length = next(int(x.split(b':', 1)[1]) for x in head.split(b'\r\n')
                          if x.lower().startswith(b'content-length:'))
            await reader.readexactly(length)
            accepted.set()
            assert await reader.read() == b''
            closed.set()
        finally:
            writer.close()
            await writer.wait_closed()

    backend = await asyncio.start_server(upstream, '127.0.0.1', 0)
    port = backend.sockets[0].getsockname()[1]
    route = proxy._Route('Red-Qwen3.8-27B-PARO-INT5', f'http://127.0.0.1:{port}/v1', 'explicit', [])
    monkeypatch.setattr(proxy, '_pick_backend', AsyncMock(return_value=route))
    monkeypatch.setattr(proxy, '_backend_model_id_cached', AsyncMock(return_value='test-model'))
    app = FastAPI()
    # Production has three @app.middleware("http") layers. Each introduces
    # asynchronous receive checkpoints, even when dispatch only calls through.
    async def middleware(request, call_next):
        return await call_next(request)
    for _ in range(3):
        app.middleware('http')(middleware)

    @app.post('/llm/v1/chat/completions')
    async def endpoint(request: Request):
        return await proxy._proxy('chat/completions', request, MagicMock(), db)

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    config = uvicorn.Config(app, log_level='critical', lifespan='off')
    server = uvicorn.Server(config)
    async with httpx.AsyncClient(timeout=5) as client:
        monkeypatch.setattr(proxy, '_client', lambda: client)
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(3):
                while not server.started:
                    await asyncio.sleep(.01)
            _, writer = await asyncio.open_connection('127.0.0.1', sock.getsockname()[1])
            body = json.dumps({'model': route.slug, 'messages': [{'role': 'user', 'content': 'private fixture'}]}).encode()
            writer.write(b'POST /llm/v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain()
            await asyncio.wait_for(accepted.wait(), 2)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(closed.wait(), 2)
            async with asyncio.timeout(2):
                while not db.usage.insert_one.await_count:
                    await asyncio.sleep(.01)
            db.usage.insert_one.assert_awaited_once()
            assert db.usage.insert_one.call_args.args[0]['metadata']['status_code'] == 499
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
            backend.close()
            await backend.wait_closed()
