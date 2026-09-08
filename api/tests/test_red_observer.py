from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import json

import pytest

from aria.infrastructure import red_observer as red
from aria.infrastructure.model_servers import RuntimeStats
from aria.api.routes import infrastructure as routes


@pytest.mark.asyncio
async def test_failed_ssh_never_refreshes_heartbeat(monkeypatch):
    proc = MagicMock(returncode=255)
    proc.communicate = AsyncMock(return_value=(b'', b''))
    monkeypatch.setattr(red.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc))
    db = MagicMock()
    assert await red.observe_once(db) is False
    db.nodes.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_gpu_identity_never_registers_node(monkeypatch):
    row = {'node_id': 'red-linux', 'hostname': 'red-linux', 'os': 'Linux',
           'hardware': {'node': 'red-linux', 'devices': []}}
    proc = MagicMock(returncode=0)
    proc.communicate = AsyncMock(return_value=(json.dumps(row).encode(), b''))
    monkeypatch.setattr(red.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc))
    db = MagicMock()
    with pytest.raises(ValueError, match='GPU identity'):
        await red.observe_once(db)
    db.nodes.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_sleeping_node_does_not_supply_live_counters():
    db = MagicMock()
    db.nodes.find_one = AsyncMock(return_value={
        'last_heartbeat_at': datetime.now(timezone.utc) - timedelta(hours=1),
        'hardware': {'devices': [{'vram_used_gib': 31.7}]},
    })
    assert await red.snapshot(db) is None
    response = await routes._with_red_hardware({'devices': []}, db)
    assert response['remote_hosts'] == [{'node': 'red-linux', 'status': 'offline', 'hardware': None}]


@pytest.mark.asyncio
async def test_utilization_includes_only_verified_remote(monkeypatch):
    good = {'slug': red.MODEL, 'state': 'running', 'onbox': False,
            'remote_identity_verified': True, 'port': 8094,
            'endpoints': {'local': 'http://127.0.0.1:8094/v1'},
            'slots': 8, 'served_ctx': 262144, 'ctx_per_slot': 262144}
    manager = MagicMock()
    manager.status = AsyncMock(return_value=[good, {**good, 'remote_identity_verified': False}])
    probe = AsyncMock(return_value=RuntimeStats(runtime_family='vllm', busy_slots=2))
    monkeypatch.setattr(routes, 'probe_runtime', probe)
    monkeypatch.setattr(routes, 'check_pi_slot_budget', lambda: None)
    result = await routes.model_server_utilization(manager, MagicMock())
    probe.assert_awaited_once()
    row = result['servers'][0]
    assert row['total_slots'] == 8 and row['free_slots'] == 6
    assert row['served_ctx'] == 262144


@pytest.mark.asyncio
async def test_red_seed_is_enabled_and_preserves_existing_profile():
    from aria.db.migrations import _seed_pi_coding_red_agent
    db = MagicMock()
    db.agents.find_one = AsyncMock(return_value=None)
    db.agents.insert_one = AsyncMock()
    await _seed_pi_coding_red_agent(db)
    row = db.agents.insert_one.await_args.args[0]
    assert row['slug'] == 'pi-coding-red'
    assert row['llm']['backend'] == 'aria' and row['llm']['model'] == red.MODEL
    assert row['model_server'] == red.MODEL
    db.agents.find_one.return_value = {'slug': 'pi-coding-red', 'enabled': False}
    await _seed_pi_coding_red_agent(db)
    db.agents.insert_one.assert_awaited_once()
