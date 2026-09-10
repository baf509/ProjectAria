from datetime import datetime, timedelta, timezone
import json
import subprocess
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from aria.infrastructure import thermals as t


def test_hwmon_units_limits_and_faults(tmp_path):
    for i in range(2):
        p = tmp_path / f'hwmon{i}'
        p.mkdir()
        for name, value in {'name': 'amdgpu', 'temp1_input': '76500',
                            'temp1_label': 'junction', 'temp1_max': '95000',
                            'temp1_crit': '110000', 'temp2_input': '85000',
                            'temp2_fault': '1', 'temp3_input': 'NaN',
                            'temp4_input': '50000', 'temp4_enable': '0'}.items():
            (p / name).write_text(value)
    sensors = t.linux_sensors(tmp_path)
    assert len(sensors) == 2
    assert len({s['id'] for s in sensors}) == 2
    assert sensors[0]['value_c'] == 76.5
    assert sensors[0]['high_c'] == 95
    assert sensors[0]['critical_c'] == 110
    assert sensors[0]['label'] == 'GPU · junction'


def test_mac_filters_invalid_sensor_without_hiding_cpu(monkeypatch):
    monkeypatch.setattr(t.subprocess, 'check_output', lambda *a, **k: json.dumps(
        {'temp': {'cpu_temp_avg': 48.25, 'gpu_temp_avg': 9.2}}))
    cpu, gpu = t.mac_sensors()
    assert cpu['value_c'] == 48.2
    assert gpu['value_c'] is None


def test_collector_timeout_is_unavailable_and_cached(monkeypatch):
    monkeypatch.setattr(t, '_cache', None)
    monkeypatch.setattr(t.sys, 'platform', 'darwin')
    calls = []
    def timeout():
        calls.append(1)
        raise subprocess.TimeoutExpired('macmon', 4)
    monkeypatch.setattr(t, 'mac_sensors', timeout)
    a, b = t.thermal_snapshot(), t.thermal_snapshot()
    assert a == b
    assert a['status'] == 'unavailable'
    assert calls == [1]


@pytest.mark.parametrize('stamp', [None, 12, 'bad', '2000-01-01T00:00:00Z', '2999-01-01T00:00:00Z'])
def test_stale_malformed_or_future_samples_are_not_live(stamp):
    result = t.host_temperatures('red-linux', {'observed_at': stamp, 'sensors': [{'value_c': 80}]})
    assert result['status'] == 'unavailable'
    assert result['sensors'] == []


def test_fresh_sample_keeps_actual_timestamp():
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(seconds=20)).isoformat()
    result = t.host_temperatures('red-linux', {'observed_at': stamp, 'sensors': [], 'status': 'available'}, now=now)
    assert result['observed_at'] == stamp
    assert result['status'] == 'available'


async def test_corsair_failure_preserves_mac_and_red_without_waking(monkeypatch):
    from aria.api.routes import infrastructure as route
    observation = {'observed_at': datetime.now(timezone.utc).isoformat(), 'status': 'available', 'sensors': []}
    monkeypatch.setattr(t, 'thermal_snapshot', lambda: observation)
    monkeypatch.setattr(route, '_local_devices', AsyncMock(side_effect=HTTPException(503, 'offline')))
    async def red(hardware, db):
        return {**hardware, 'remote_hosts': [{'node': 'red-linux', 'hardware': {'temperatures': observation}}]}
    monkeypatch.setattr(route, '_with_red_hardware', red)
    result = await route.list_devices(db=None)
    hosts = {h['node']: h['status'] for h in result['temperature_hosts']}
    assert hosts == {'bens-macbook-pro': 'available', 'corsair-ai': 'unavailable', 'red-linux': 'available', 'ridge': 'unavailable'}
