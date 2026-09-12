import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from aria.shells.capture import CaptureBuffer, drain_output
from aria.shells.screen_stream import ScreenHub, ScreenNotifier


@pytest.mark.asyncio
async def test_shared_reader_coalesces_and_drops_old_screens(tmp_path):
    hub = ScreenHub(interval=0.01, fallback=10, path=tmp_path / 's.sock')
    fetch = AsyncMock(return_value='initial')
    try:
        async with hub.subscribe('shell', fetch) as a, hub.subscribe('shell', fetch) as b:
            assert (await asyncio.wait_for(a.get(), 1))['screen'] == 'initial'
            assert (await b.get())['screen'] == 'initial'
            assert fetch.await_count == 1
            for value in ['second', 'third']:
                fetch.return_value = value
                for _ in range(100):
                    hub.notify('shell')
                assert (await asyncio.wait_for(a.get(), 1))['screen'] == value
            assert fetch.await_count == 3
            assert b.qsize() == 1
            assert (await b.get())['screen'] == 'third'
        assert hub.watches == {}
        count = fetch.await_count
        await asyncio.sleep(0.04)
        assert fetch.await_count == count
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_socket_hints_fallback_recovery_and_lock(tmp_path, monkeypatch):
    short_dir = tempfile.TemporaryDirectory(prefix='aria-sse-', dir='/tmp')
    path = Path(short_dir.name) / 's.sock'
    monkeypatch.setenv('ARIA_SHELL_SCREEN_SOCKET', str(path))
    hub = ScreenHub(interval=0.001, fallback=0.04, path=path)
    other = ScreenHub(path=path)
    fetch = AsyncMock(return_value='one')
    notifier = ScreenNotifier()
    try:
        async with hub.subscribe('shell', fetch) as queue:
            await queue.get()
            inode = path.stat().st_ino
            await other.start()
            assert other.transport is None
            assert path.stat().st_ino == inode
            fetch.return_value = 'two'
            notifier.notify('shell')
            assert (await asyncio.wait_for(queue.get(), 0.5))['screen'] == 'two'
            fetch.side_effect = RuntimeError('pane unavailable')
            assert 'error' in await asyncio.wait_for(queue.get(), 0.5)
            fetch.side_effect = None
            fetch.return_value = 'recovered'
            assert (await asyncio.wait_for(queue.get(), 0.5))['screen'] == 'recovered'
        async with hub.subscribe('shell', fetch) as queue:
            assert (await asyncio.wait_for(queue.get(), 0.5))['screen'] == 'recovered'
    finally:
        notifier.close()
        await other.stop()
        await hub.stop()
    assert not path.exists()
    short_dir.cleanup()


@pytest.mark.asyncio
async def test_capture_drains_without_database_and_preserves_split_unicode(tmp_path, monkeypatch):
    monkeypatch.setenv('ARIA_SHELL_SCREEN_SOCKET', str(tmp_path / 'absent.sock'))
    notifier = ScreenNotifier()
    buffer = CaptureBuffer(max_bytes=256, max_items=4)
    reader = asyncio.StreamReader()
    task = asyncio.create_task(drain_output(reader, buffer, 'shell', notifier))
    try:
        for _ in range(100):
            reader.feed_data(b'x' * 64)
            await asyncio.sleep(0)
        # The writer has never run, simulating arbitrarily slow persistence.
        assert buffer.dropped > 0
        assert buffer.bytes <= 256
        buffer.take(100)
        emoji = '🙂'.encode()
        reader.feed_data(emoji[:2])
        await asyncio.sleep(0)
        reader.feed_data(emoji[2:])
        reader.feed_eof()
        await asyncio.wait_for(task, 1)
        assert ''.join(r['text_raw'] for r in buffer.take(100)) == '🙂'
    finally:
        notifier.close()
        task.cancel()


@pytest.mark.asyncio
async def test_screen_route_uses_one_local_capture_and_cleans_up(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from aria.api.routes.shells import stream_shell_screen
    import aria.shells.screen_stream as module
    hub = ScreenHub(interval=0.001, fallback=10, path=tmp_path / 'route.sock')
    monkeypatch.setattr(module, 'screen_hub', hub)
    service = SimpleNamespace(
        resolve_shell_name=AsyncMock(return_value='claude-test'),
        get_shell=AsyncMock(return_value=SimpleNamespace(host='mac')),
        _shell_is_remote=lambda _: False,
        tmux=SimpleNamespace(capture_screen=AsyncMock(return_value='visible')),
    )
    response = await stream_shell_screen('claude-test', service)
    frames = response.body_iterator
    try:
        frame = await asyncio.wait_for(anext(frames), 1)
        assert frame['event'] == 'screen'
        assert 'visible' in frame['data']
        service.tmux.capture_screen.assert_awaited_once_with('claude-test')
        service.get_shell.assert_awaited_once()
    finally:
        await frames.aclose()
        assert not hub.watches
        await hub.stop()
