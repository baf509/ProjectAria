"""Read-only NVIDIA telemetry, without importing CUDA or allocating GPU memory.

Device reads are fresh for memory gates. Process reads share a one-second
sample so walking a service process tree does not spawn nvidia-smi per PID.
Unknown/unavailable telemetry is None, never an empty 24 GiB card.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import os
import re
import subprocess
import threading
import time


@dataclass(frozen=True)
class NvidiaDevice:
    uuid: str
    pci_address: str
    name: str
    total_mib: int
    used_mib: int


def _query(kind: str, fields: str) -> str | None:
    if not os.path.isfile("/proc/driver/nvidia/version"):
        return None  # In particular: never probe the Mac control plane.
    try:
        return subprocess.check_output(
            ["/usr/bin/nvidia-smi", f"--query-{kind}={fields}",
             "--format=csv,noheader,nounits"],
            text=True, stderr=subprocess.DEVNULL, timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def parse_devices(text: str) -> list[NvidiaDevice]:
    devices = []
    seen = set()
    for row in csv.reader(text.splitlines(), skipinitialspace=True):
        if len(row) != 5:
            raise ValueError("invalid NVIDIA device row")
        uuid, pci, name, total, used = (value.strip() for value in row)
        if (not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", uuid) or uuid in seen
                or not re.fullmatch(r"[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", pci)
                or not name or not total.isdigit() or not used.isdigit()
                or not 0 <= int(used) <= int(total) or int(total) == 0):
            raise ValueError("invalid NVIDIA identity or memory counter")
        seen.add(uuid)
        devices.append(NvidiaDevice(uuid, pci.lower(), name, int(total), int(used)))
    return devices


def devices() -> list[NvidiaDevice] | None:
    text = _query("gpu", "uuid,pci.bus_id,name,memory.total,memory.used")
    if text is None:
        return None
    try:
        return parse_devices(text)
    except ValueError:
        return None


def parse_processes(text: str) -> dict[tuple[int, str], int]:
    result = {}
    for row in csv.reader(text.splitlines(), skipinitialspace=True):
        if len(row) != 3:
            raise ValueError("invalid NVIDIA process row")
        pid, uuid, used = (value.strip() for value in row)
        if (not pid.isdigit() or int(pid) <= 0 or not used.isdigit()
                or not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", uuid)):
            raise ValueError("invalid NVIDIA process counter")
        key = (int(pid), uuid)
        if key in result:
            raise ValueError("duplicate NVIDIA process/device observation")
        result[key] = int(used) * 1024**2
    return result


_process_lock = threading.Lock()
_process_cache: tuple[float, dict[tuple[int, str], int] | None] | None = None


def process_bytes(pid: int) -> int | None:
    global _process_cache
    with _process_lock:
        now = time.monotonic()
        if _process_cache is None or now - _process_cache[0] >= 1.0:
            text = _query("compute-apps", "pid,gpu_uuid,used_gpu_memory")
            try:
                rows = parse_processes(text) if text is not None else None
            except ValueError:
                rows = None
            _process_cache = (time.monotonic(), rows)
        rows = _process_cache[1]
    if rows is None:
        return None
    # This logical pool is currently qualified for one NVIDIA adapter only.
    # Do not merge memory on unrelated cards into apparently contiguous VRAM.
    held = [size for (owner, _), size in rows.items() if owner == pid]
    return held[0] if len(held) == 1 else (0 if not held else None)
