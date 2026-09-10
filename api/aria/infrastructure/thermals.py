"""Bounded, read-only temperature collection; standalone for restricted hosts.

The canonical copy is in ProjectAria. Deploy this same file beside Red's
status.py; it requires only Python's standard library and no new SSH verb.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time


def _text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ''


def _temperature(value, scale=1, minimum=-40):
    try:
        number = float(value) / scale
        return round(number, 1) if math.isfinite(number) and minimum <= number <= 150 else None
    except (TypeError, ValueError):
        return None


def linux_sensors(root=Path('/sys/class/hwmon')) -> list[dict]:
    rows = []
    for monitor in sorted(root.glob('hwmon*')):
        chip = _text(monitor / 'name') or monitor.name
        device = (monitor / 'device').resolve().name if (monitor / 'device').exists() else monitor.name
        kind = ('cpu' if chip in {'k10temp', 'coretemp', 'zenpower', 'cpu_thermal'} else
                'gpu' if chip in {'amdgpu', 'nouveau'} else
                'storage' if chip == 'nvme' else 'memory' if chip.startswith('spd') else 'other')
        for path in sorted(monitor.glob('temp*_input')):
            stem = path.name.removesuffix('_input')
            if _text(monitor / f'{stem}_fault') == '1' or _text(monitor / f'{stem}_enable') == '0':
                continue
            value = _temperature(_text(path), 1000)
            if value is None:
                continue
            label = _text(monitor / f'{stem}_label') or stem
            title = {'cpu': 'CPU', 'gpu': 'GPU', 'storage': 'Storage', 'memory': 'Memory'}.get(kind, chip)
            rows.append({'id': f'{chip}:{device}:{stem}', 'kind': kind,
                         'label': f'{title} · {label}', 'device': device, 'value_c': value,
                         'high_c': _temperature(_text(monitor / f'{stem}_max'), 1000),
                         'critical_c': _temperature(_text(monitor / f'{stem}_crit'), 1000),
                         'source': 'Linux hwmon'})
    return rows


def nvidia_sensors() -> list[dict]:
    if not Path('/proc/driver/nvidia/version').is_file():
        return []
    try:
        output = subprocess.check_output(
            ['/usr/bin/nvidia-smi', '--query-gpu=uuid,name,temperature.gpu',
             '--format=csv,noheader,nounits'], text=True, stderr=subprocess.DEVNULL, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return []
    result = []
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if len(row) != 3:
            continue
        identity, label, raw = (value.strip() for value in row)
        value = _temperature(raw)
        if value is not None:
            result.append({'id': identity, 'kind': 'gpu', 'label': label, 'device': identity,
                           'value_c': value, 'high_c': None, 'critical_c': None, 'source': 'nvidia-smi'})
    return result


def mac_sensors() -> list[dict]:
    output = subprocess.check_output(
        ['/opt/homebrew/bin/macmon', 'pipe', '-s', '1', '-i', '100'],
        text=True, stderr=subprocess.DEVNULL, timeout=4)
    reading = json.loads(output.strip().splitlines()[-1])['temp']
    # Some inactive Apple GPU sensors yield bogus single-digit averages. Keep
    # the missing value explicit instead of displaying it as a cool, healthy GPU.
    return [{'id': f'mac-{kind}', 'kind': kind, 'label': f'{kind.upper()} · sensor average',
             'device': 'Apple Silicon', 'value_c': _temperature(reading.get(f'{kind}_temp_avg'), minimum=10),
             'high_c': None, 'critical_c': None, 'source': 'macmon'} for kind in ('cpu', 'gpu')]


_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


def thermal_snapshot() -> dict:
    global _cache
    with _lock:
        now = time.monotonic()
        if _cache and now - _cache[0] < 10:
            return _cache[1]
        try:
            sensors = mac_sensors() if sys.platform == 'darwin' else linux_sensors() + nvidia_sensors()
        except (OSError, TypeError, AttributeError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
            sensors = []
        result = {'observed_at': datetime.now(timezone.utc).isoformat(), 'sensors': sensors,
                  'status': 'available' if any(row['value_c'] is not None for row in sensors) else 'unavailable'}
        _cache = (time.monotonic(), result)
        return result


def host_temperatures(node: str, observation: dict | None, *, max_age=45, now=None) -> dict:
    """Never let cached or future-dated readings masquerade as live sensors."""
    now = now or datetime.now(timezone.utc)
    try:
        stamp = datetime.fromisoformat(observation['observed_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (now - stamp).total_seconds()
        if not -5 <= age <= max_age:
            raise ValueError('stale sample')
        return {**observation, 'node': node, 'max_age_seconds': max_age}
    except (AttributeError, TypeError, KeyError, ValueError):
        return {'node': node, 'status': 'unavailable', 'observed_at': None,
                'max_age_seconds': max_age, 'sensors': []}
