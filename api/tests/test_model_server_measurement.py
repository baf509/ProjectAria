"""Tests for OS-level memory measurement of model servers.

Context: `spec.resident_gib` is a hand-maintained SWAG and it goes stale
silently. DS4 declared 86.5 GiB (measured at -c 131072) while actually holding
94.08 after moving to -c 262144 with a 4 GiB prompt cache — a 7.6 GiB
under-count feeding the very gate that is supposed to prevent overcommit.

The properties that matter here are about NOT trusting the declaration when a
real number is available, and about never summing CPU-only servers into a GPU
total.
"""

from unittest.mock import AsyncMock, patch

import pytest

from aria.infrastructure.model_servers import (
    _gtt_bytes_for_pid,
    _rss_bytes_for_pid,
    measure_resident_gib,
)


def _spec(**kw):
    from aria.infrastructure.model_servers import ModelServerSpec
    base = dict(
        slug="test", description="", runtime_repo="", runtime_ref="",
        backend_device="ROCm0", systemd_unit="test.service",
    )
    base.update(kw)
    return ModelServerSpec(**base)


@pytest.mark.asyncio
async def test_gpu_resident_server_is_measured_from_the_kfd_tree():
    """GTT allocations are invisible to RSS and to cgroup accounting on this
    unified-memory box — the kfd tree is the only signal that sees them."""
    spec = _spec(gtt_resident=True)
    with patch("aria.infrastructure.model_servers._server_pid",
               AsyncMock(return_value=4242)), \
         patch("aria.infrastructure.model_servers._gtt_bytes_for_pid",
               return_value=94 * 1024**3):
        assert await measure_resident_gib(spec) == pytest.approx(94.0)


@pytest.mark.asyncio
async def test_cpu_only_server_is_measured_from_rss_not_gtt():
    """gemma-aux holds ~3 GiB of host RAM and ZERO GTT. Measuring it via the
    kfd tree would report nothing; summing it into a GPU total is a category
    error that made ARIA's headline number right only by luck."""
    spec = _spec(gtt_resident=False)
    with patch("aria.infrastructure.model_servers._server_pid",
               AsyncMock(return_value=4242)), \
         patch("aria.infrastructure.model_servers._rss_bytes_for_pid",
               return_value=3 * 1024**3) as rss, \
         patch("aria.infrastructure.model_servers._gtt_bytes_for_pid") as gtt:
        assert await measure_resident_gib(spec) == pytest.approx(3.0)
        rss.assert_called_once()
        gtt.assert_not_called()


@pytest.mark.asyncio
async def test_stopped_server_measures_none_so_callers_fall_back():
    spec = _spec()
    with patch("aria.infrastructure.model_servers._server_pid",
               AsyncMock(return_value=None)):
        assert await measure_resident_gib(spec) is None


@pytest.mark.asyncio
async def test_gpu_declared_server_holding_no_gpu_memory_falls_back_to_rss():
    """A server declared GPU-resident that has no kfd entry is either CPU-only
    in practice or mid-load. Report its RSS rather than nothing, so the
    mismatch is visible instead of silently absent."""
    spec = _spec(gtt_resident=True)
    with patch("aria.infrastructure.model_servers._server_pid",
               AsyncMock(return_value=4242)), \
         patch("aria.infrastructure.model_servers._gtt_bytes_for_pid",
               return_value=None), \
         patch("aria.infrastructure.model_servers._rss_bytes_for_pid",
               return_value=2 * 1024**3):
        assert await measure_resident_gib(spec) == pytest.approx(2.0)


def test_gtt_reader_returns_none_for_a_pid_with_no_kfd_entry():
    """Not using the GPU at all must read as None, not 0 — 0 would look like a
    measured, empty footprint and mask a stopped or CPU-only server."""
    assert _gtt_bytes_for_pid(999999999) is None


def test_rss_reader_returns_none_for_a_dead_pid():
    assert _rss_bytes_for_pid(999999999) is None


def test_rss_reader_works_on_our_own_process():
    import os
    v = _rss_bytes_for_pid(os.getpid())
    assert v is not None and v > 1024 * 1024, "should read a real RSS for self"
