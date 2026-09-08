from unittest.mock import AsyncMock, patch
import subprocess

import pytest

from aria.infrastructure import gpu_devices as gd
from aria.infrastructure import nvidia_gpu as ng

UUID = "GPU-ab0d4675-0482-6860-8dd6-8b17ad126e6d"
ROW = f"{UUID}, 00000000:C4:00.0, NVIDIA GeForce RTX 3090, 24576, 512"


def test_real_nvidia_row_and_pool_are_not_r9700_or_halo():
    with patch.object(ng, "_query", return_value=ROW):
        card = ng.devices()[0]
        pool = gd.read_pool(gd.POOL_NVIDIA)
    assert card.pci_address == "00000000:c4:00.0"
    assert pool.total_gib == 24
    assert pool.used_gib == 0.5
    assert pool.pool not in (gd.POOL_HALO, gd.POOL_R9700)
    assert UUID in pool.source


@pytest.mark.parametrize("text", [None, "", ROW.replace("512", "N/A"),
                                 ROW.replace("512", "25000"), ROW + "\n" + ROW])
def test_unavailable_invalid_or_ambiguous_is_not_empty_vram(text):
    with patch.object(ng, "_query", return_value=text):
        assert gd.read_pool(gd.POOL_NVIDIA) is None


def test_two_cards_are_not_one_contiguous_pool():
    other = ROW.replace("ab0d4675", "cb0d4675").replace("C4:", "C5:")
    with patch.object(ng, "_query", return_value=ROW + "\n" + other):
        assert len(ng.devices()) == 2
        assert gd.read_pool(gd.POOL_NVIDIA) is None


def test_nvidia_query_is_bounded_and_never_runs_on_mac():
    with patch.object(ng.os.path, "isfile", return_value=False), \
            patch.object(ng.subprocess, "check_output") as query:
        assert ng.devices() is None
        query.assert_not_called()
    with patch.object(ng.os.path, "isfile", return_value=True), \
            patch.object(ng.subprocess, "check_output", side_effect=subprocess.TimeoutExpired("nvidia-smi", 2)) as query:
        assert ng.devices() is None
        assert query.call_args.kwargs["timeout"] == 2.0


def test_process_tree_shares_one_bounded_sample_then_refreshes():
    sample = f"123, {UUID}, 2048\n124, {UUID}, 16"
    with patch.object(ng, "_process_cache", None), \
            patch.object(ng.time, "monotonic", return_value=1.0) as clock, \
            patch.object(ng, "_query", return_value=sample) as query:
        assert ng.process_bytes(123) == 2 * 1024**3
        assert ng.process_bytes(124) == 16 * 1024**2
        assert ng.process_bytes(999) == 0
        assert query.call_count == 1
        clock.return_value = 2.1
        query.return_value = None
        assert ng.process_bytes(123) is None  # No stale successful reading.
        assert query.call_count == 2


@pytest.mark.parametrize("text", [f"123, {UUID}, N/A", f"0, {UUID}, 1",
                                 f"123, {UUID}, 1\n123, {UUID}, 1"])
def test_invalid_process_counter_is_unknown(text):
    with patch.object(ng, "_process_cache", None), patch.object(ng, "_query", return_value=text):
        assert ng.process_bytes(123) is None


def test_nvidia_pid_measurement_survives_unreadable_amd_fdinfo():
    with patch.object(gd, "discover_devices", return_value=[]), \
            patch.object(gd.os, "listdir", side_effect=PermissionError), \
            patch.object(ng, "process_bytes", return_value=2 * 1024**3):
        assert gd.process_gpu_bytes(123) == {gd.POOL_NVIDIA: 2 * 1024**3}


def test_discrete_cards_never_overlap_each_other_or_double_count_system_ram():
    def pool(name):
        return gd.MemoryPool(name, name, 1, 24, "test")
    with patch.object(gd, "read_pool", side_effect=pool):
        rows = {row["pool"]: row for row in gd.pool_snapshot()}
    assert rows[gd.POOL_NVIDIA]["overlaps"] == []
    assert rows[gd.POOL_R9700]["overlaps"] == []
    assert rows[gd.POOL_HALO]["overlaps"] == [gd.POOL_HOST]
    assert rows[gd.POOL_HOST]["overlaps"] == [gd.POOL_HALO]


@pytest.mark.asyncio
async def test_mac_device_panel_reads_restricted_corsair_observation_not_local_sysfs():
    from aria.api.routes import infrastructure as route
    observation = {"node": "corsair-ai", "devices": [{"card": UUID}], "pools": [], "system": None}
    with patch.object(route, "_corsair_forward_mode", return_value=True), \
            patch.object(route, "_corsair_actuate", AsyncMock(return_value={"hardware": observation})) as remote, \
            patch.object(route, "device_snapshot") as local:
        # The public route adds independent Red telemetry through its injected
        # database. This test isolates the restricted Corsair observation.
        assert await route._local_devices() is observation
        remote.assert_awaited_once_with("status", "Qwen3.8-Flash-Next-CUDA-Halo-Candidate")
        local.assert_not_called()


@pytest.mark.asyncio
async def test_old_actuator_is_unavailable_not_an_empty_gpu_host():
    from fastapi import HTTPException
    from aria.api.routes import infrastructure as route
    with patch.object(route, "_corsair_forward_mode", return_value=True), \
            patch.object(route, "_corsair_actuate", AsyncMock(return_value={})), \
            pytest.raises(HTTPException) as error:
        await route._local_devices()
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_unavailable_corsair_keeps_independent_red_observation():
    from aria.api.routes import infrastructure as route
    from aria.infrastructure import red_observer
    red = {"hardware": {"node": "red-linux", "devices": [{"card": "R9700"}]}}
    with patch.object(route, "_corsair_forward_mode", return_value=True), \
            patch.object(route, "_corsair_actuate", AsyncMock(return_value={})), \
            patch.object(red_observer, "snapshot", AsyncMock(return_value=red)):
        result = await route.list_devices(db=None)
    assert result["node"] == "corsair-ai"
    assert result["telemetry_error"]
    assert result["remote_hosts"][0]["hardware"] == red["hardware"]
