"""Operator-registered Halogen backend: slow /health probe budget and Mac-side
exclusivity for dynamic model_servers rows.

Halogen's /health round-trips a PONG through its engine and answers in ~1.02 s
when idle, so the shared 0.75 s forwarded probe reported it exited. The Corsair
actuator only resolves static slugs, so a dynamic row's exclusivity must be
enforced on the Mac before a Corsair start is sent.
"""
from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from aria.infrastructure import model_servers as ms
from aria.infrastructure.model_servers import ModelServerManager, ModelServerSafetyError

INCUMBENT = "Qwen3.8-Flash-Next-CUDA-Halo-Candidate"
HALOGEN_DOC = {
    "slug": "Halogen-Qwen3.8-Flash-Next-W4B-Halo",
    "port": 8109,
    "container_name": "halogen-flash",
    "resident_gib": 124.0,
    "runtime_family": "halogen",
    "health_timeout_s": 3.0,
    "exclusive_with": [INCUMBENT, "Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K"],
}


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


class _Collection:
    def __init__(self, docs):
        self._docs = docs

    def find(self, _query=None, *_args, **_kwargs):
        return _Cursor(self._docs)


class _Db:
    def __init__(self, docs):
        self.model_servers = _Collection(docs)


@contextlib.asynccontextmanager
async def _slow_health_server(delay: float):
    async def handle(reader, writer):
        await reader.readline()
        await asyncio.sleep(delay)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def test_dynamic_row_carries_runtime_family_health_budget_and_exclusivity():
    spec = ModelServerManager._spec_from_doc(HALOGEN_DOC)
    assert spec.runtime_family == "halogen"
    assert spec.health_timeout_s == 3.0
    assert INCUMBENT in spec.exclusive_with


def test_rows_without_the_new_fields_keep_previous_behaviour():
    spec = ModelServerManager._spec_from_doc({"slug": "pulled", "port": 8199})
    assert spec.runtime_family == "llamacpp"
    assert spec.health_timeout_s == 0.75
    assert spec.exclusive_with == ()
    # The probe call for default deployments is unchanged: no timeout override.
    assert ms._health_timeout_kwargs([spec]) == {}
    assert ms._health_timeout_kwargs([ms._BY_SLUG[INCUMBENT]]) == {}


@pytest.mark.asyncio
async def test_one_second_health_reads_running_at_three_seconds_and_exited_at_default():
    async with _slow_health_server(1.0) as port:
        slow = ModelServerManager._spec_from_doc(dict(HALOGEN_DOC, port=port))
        default = ModelServerManager._spec_from_doc({"slug": "default-budget", "port": port})

        assert (await ms._forwarded_fleet_states([slow]))[slow.slug] == "running"
        assert (await ms._forwarded_fleet_states([default]))[default.slug] == "exited"
        assert await ms._forwarded_endpoint_open(slow) is True
        assert await ms._forwarded_endpoint_open(default) is False


@pytest.mark.asyncio
async def test_incumbent_start_is_refused_while_halogen_runs():
    manager = ModelServerManager()
    actuate = AsyncMock(return_value={"slug": INCUMBENT, "state": "starting"})
    probe = AsyncMock(return_value=(True, None))
    with patch.object(ms, "_corsair_forward_mode", return_value=True), \
         patch.object(ms, "_corsair_actuate", actuate), \
         patch.object(ms, "_forwarded_endpoint_status", probe):
        with pytest.raises(ModelServerSafetyError, match="mutually exclusive with running server"):
            await manager.start(INCUMBENT, db=_Db([dict(HALOGEN_DOC)]))
    actuate.assert_not_awaited()
    assert probe.await_args.args == (8109,)
    assert probe.await_args.kwargs["timeout"] == 3.0


@pytest.mark.asyncio
async def test_incumbent_start_proceeds_when_halogen_is_not_serving():
    manager = ModelServerManager()
    actuate = AsyncMock(return_value={"slug": INCUMBENT, "state": "starting"})
    with patch.object(ms, "_corsair_forward_mode", return_value=True), \
         patch.object(ms, "_corsair_actuate", actuate), \
         patch.object(ms, "_forwarded_endpoint_status", AsyncMock(return_value=(False, None))):
        result = await manager.start(INCUMBENT, db=_Db([dict(HALOGEN_DOC)]))
    assert result["state"] == "starting"
    actuate.assert_awaited_once_with("start", INCUMBENT, force=False)


@pytest.mark.asyncio
async def test_unrelated_start_is_not_blocked_by_halogen():
    manager = ModelServerManager()
    actuate = AsyncMock(return_value={"state": "starting"})
    probe = AsyncMock(return_value=(True, None))
    with patch.object(ms, "_corsair_forward_mode", return_value=True), \
         patch.object(ms, "_corsair_actuate", actuate), \
         patch.object(ms, "_forwarded_endpoint_status", probe):
        await manager.start("Qwen3.5-9B-Aux-CPU", db=_Db([dict(HALOGEN_DOC)]))
    actuate.assert_awaited_once()
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_halogen_runtime_is_not_probed_as_llamacpp():
    spec = ModelServerManager._spec_from_doc(HALOGEN_DOC)
    with patch.object(ms, "_probe_llamacpp", AsyncMock()) as llamacpp:
        assert await ms.probe_runtime(spec) is None
    llamacpp.assert_not_awaited()
    assert ms._runtime_family_from_models({"data": [{"owned_by": "halogen"}]}) == "halogen"
