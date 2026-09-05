"""Real TCP checks: fresh upstream sockets and no generation replay."""
import asyncio

import httpx
import pytest

from aria.api.routes import llm_proxy as proxy


@pytest.mark.asyncio
async def test_transport_trace_discards_private_payloads_and_unknown_events():
    trace = proxy._UpstreamTrace()
    await trace.observe('connection.connect_tcp.started', {'credential': 'private-value'})
    await trace.observe('untrusted-private-event', {'body': 'private-value'})
    assert trace.stages == {'connection.connect_tcp.started'}
    assert 'private' not in repr(vars(trace))


@pytest.mark.asyncio
async def test_completed_posts_and_streams_do_not_reuse_idle_sockets(monkeypatch):
    connections, requests, handlers = [], [], set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        connection = len(connections)
        connections.append(connection)
        try:
            while True:
                try:
                    head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
                except asyncio.IncompleteReadError:
                    break
                length = next(int(line.split(b':', 1)[1]) for line in head.split(b'\r\n')
                              if line.lower().startswith(b'content-length:'))
                assert await reader.readexactly(length) == b'fixture'
                requests.append(connection)
                # The peer explicitly permits reuse; the gateway must decline it.
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n'
                             b'Connection: keep-alive\r\nKeep-Alive: timeout=5\r\n\r\n{}')
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/fixture'
    monkeypatch.setattr(proxy, '_shared_client', None)
    try:
        client = proxy._client()
        for index in range(6):
            if index % 2:
                async with client.stream('POST', url, content=b'fixture') as response:
                    assert await response.aread() == b'{}'
            else:
                assert (await client.post(url, content=b'fixture')).content == b'{}'
            assert proxy._client() is client
        assert requests == list(range(6))
    finally:
        await proxy.close_client()
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.wait_for(asyncio.gather(*list(handlers)), 3)


@pytest.mark.asyncio
async def test_disconnected_post_is_not_replayed(monkeypatch):
    accepted = []
    finished = asyncio.Event()

    async def drop(reader, writer):
        try:
            await reader.readuntil(b'\r\n\r\n')
            accepted.append(True)
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    server = await asyncio.start_server(drop, '127.0.0.1', 0)
    url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/fixture'
    monkeypatch.setattr(proxy, '_shared_client', None)
    try:
        with pytest.raises(httpx.RemoteProtocolError):
            await proxy._client().post(url, content=b'fixture')
        await asyncio.wait_for(finished.wait(), 3)
        assert len(accepted) == 1
    finally:
        await proxy.close_client()
        server.close()
        await server.wait_closed()
