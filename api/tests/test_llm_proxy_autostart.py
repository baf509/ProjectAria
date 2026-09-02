"""Tests for on-demand model start in the OpenAI passthrough.

The point of this path: a consumer should be able to treat ARIA like an ordinary
provider — pick a model and use it — rather than pick a model, find it stopped,
go start it elsewhere, come back.

The safety properties matter more than the happy path. Starting a model here can
EVICT a running one and block the request for minutes, so it must fire only when
the caller named a specific model, and it must free memory by stopping conflicts
rather than by bypassing the gates that stop us overcommitting the box.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.api.routes.llm_proxy import (
    _Route,
    _autostart,
    _names_a_model,
    _norm_slug,
    _proxy,
)


def test_norm_slug_matches_gguf_filename_to_slug():
    assert _norm_slug("Ling-3.0-flash-Q5_K_M.gguf") == _norm_slug("Ling-3.0-flash-Q5_K_M")
    assert _norm_slug("  LING-3.0-FLASH-Q5_K_M  ") == _norm_slug("ling-3.0-flash-q5_k_m")


@pytest.mark.parametrize("alias", ["auto", "aria", "aria-resident", "default", "", None])
def test_auto_aliases_do_not_count_as_naming_a_model(alias):
    """The alias means 'whatever is up' — it must never trigger an eviction."""
    assert _names_a_model(alias) is False


@pytest.mark.parametrize("named", ["Ling-3.0-flash-Q5_K_M", "DS4-0731-ROCMFPX-affine-128k"])
def test_concrete_names_count_as_naming_a_model(named):
    assert _names_a_model(named) is True


def _manager(servers):
    m = MagicMock()
    m.status = AsyncMock(return_value=servers)
    m.start = AsyncMock(return_value={"state": "running"})
    m.stop = AsyncMock(return_value={"state": "exited"})
    return m


@pytest.mark.asyncio
async def test_stops_exclusive_conflict_then_starts_without_force():
    """force=True would skip the live-GTT projection — the last gate against
    overcommitting the box. Conflicts must be freed by stopping them."""
    servers = [
        {"slug": "Ling-3.0-flash-Q5_K_M", "state": "exited", "model_file": "a/Ling-3.0-flash-Q5_K_M.gguf"},
        {"slug": "DS4-0731-ROCMFPX-affine-128k", "state": "running", "model_file": "b/ds4.gguf"},
    ]
    manager = _manager(servers)
    spec = MagicMock(onbox=True, exclusive_with=("DS4-0731-ROCMFPX-affine-128k",))

    with patch.dict(
        "aria.infrastructure.model_servers._BY_SLUG",
        {"Ling-3.0-flash-Q5_K_M": spec},
        clear=False,
    ):
        ok = await _autostart(manager, MagicMock(), "Ling-3.0-flash-Q5_K_M")

    assert ok is True
    manager.stop.assert_awaited_once()
    assert manager.stop.await_args.args[0] == "DS4-0731-ROCMFPX-affine-128k"
    manager.start.assert_awaited_once()
    assert "force" not in manager.start.await_args.kwargs, "must not bypass the GTT gate"


@pytest.mark.asyncio
async def test_does_not_stop_a_conflict_that_is_already_stopped():
    servers = [
        {"slug": "Ling-3.0-flash-Q5_K_M", "state": "exited", "model_file": ""},
        {"slug": "DS4-0731-ROCMFPX-affine-128k", "state": "exited", "model_file": ""},
    ]
    manager = _manager(servers)
    spec = MagicMock(onbox=True, exclusive_with=("DS4-0731-ROCMFPX-affine-128k",))

    with patch.dict("aria.infrastructure.model_servers._BY_SLUG",
                    {"Ling-3.0-flash-Q5_K_M": spec}, clear=False):
        ok = await _autostart(manager, MagicMock(), "Ling-3.0-flash-Q5_K_M")

    assert ok is True
    manager.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_aborts_without_starting_if_a_conflict_will_not_stop():
    """Half-freeing memory and starting anyway is how you OOM the box."""
    servers = [
        {"slug": "Ling-3.0-flash-Q5_K_M", "state": "exited", "model_file": ""},
        {"slug": "DS4-0731-ROCMFPX-affine-128k", "state": "running", "model_file": ""},
    ]
    manager = _manager(servers)
    manager.stop = AsyncMock(side_effect=RuntimeError("unit refused to stop"))
    spec = MagicMock(onbox=True, exclusive_with=("DS4-0731-ROCMFPX-affine-128k",))

    with patch.dict("aria.infrastructure.model_servers._BY_SLUG",
                    {"Ling-3.0-flash-Q5_K_M": spec}, clear=False):
        ok = await _autostart(manager, MagicMock(), "Ling-3.0-flash-Q5_K_M")

    assert ok is False
    manager.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_model_name_starts_nothing():
    manager = _manager([{"slug": "Ling-3.0-flash-Q5_K_M", "state": "exited", "model_file": ""}])
    ok = await _autostart(manager, MagicMock(), "gpt-4")
    assert ok is False
    manager.start.assert_not_awaited()
    manager.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_offbox_model_is_not_started():
    servers = [{"slug": "Ridge-Qwen3.6-35B-A3B", "state": "exited", "model_file": ""}]
    manager = _manager(servers)
    spec = MagicMock(onbox=False, exclusive_with=())

    with patch.dict("aria.infrastructure.model_servers._BY_SLUG",
                    {"Ridge-Qwen3.6-35B-A3B": spec}, clear=False):
        ok = await _autostart(manager, MagicMock(), "Ridge-Qwen3.6-35B-A3B")

    assert ok is False
    manager.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_rewrites_aria_slug_to_backend_model_id():
    """ARIA's routing slug must never be forwarded as a vLLM model id."""
    request = MagicMock()
    request.body = AsyncMock(return_value=(
        b'{"model":"Qwen3.8-27B-R9700-Radiance",'
        b'"messages":[{"role":"user","content":"hello"}]}'
    ))
    request.headers = {}

    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "application/json"}
    response.json.return_value = {"choices": []}
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    route = _Route(
        "Qwen3.8-27B-R9700-Radiance",
        "http://127.0.0.1:8080/v1",
        "requested",
        [],
    )
    with (
        patch("aria.api.routes.llm_proxy._pick_backend", AsyncMock(return_value=route)),
        patch(
            "aria.api.routes.llm_proxy._backend_model_id",
            AsyncMock(return_value="qwen3.8-27b-r9700"),
        ),
        patch("aria.api.routes.llm_proxy._client", return_value=client),
    ):
        result = await _proxy("chat/completions", request, MagicMock(), MagicMock())

    assert result.status_code == 200
    sent = client.post.await_args.kwargs["content"]
    assert b'"model": "qwen3.8-27b-r9700"' in sent
    assert b'"model": "Qwen3.8-27B-R9700-Radiance"' not in sent
