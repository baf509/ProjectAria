"""Observe Red through its restricted SSH status capability, without waking it.

The Mac owns this observer. Red hosts only the read-only collector and inference;
it receives no Aria database credential or general coding/command capability.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from aria.config import settings

NODE = 'red-linux'
MODEL = 'Red-Qwen3.8-27B-MXFP4'
SSH_CONFIG = '/Users/ben/Services/config/red-model-ssh.conf'
logger = logging.getLogger(__name__)


async def observe_once(db):
    proc = await asyncio.create_subprocess_exec(
        'ssh', '-F', SSH_CONFIG, 'red-linux-model', 'status',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        return False  # Failed reads never refresh a heartbeat or wake the host.
    row = json.loads(stdout)
    if row.get('node_id') != NODE or row.get('hostname') != NODE or row.get('os') != 'Linux':
        raise ValueError('Unexpected Red SSH identity')
    hardware = row.get('hardware', {})
    expected = {'0000:03:00.0': '75c082be5ff9300b', '0000:06:00.0': '2531d39b0ad846ee'}
    observed = {d.get('pci_address'): d.get('uuid') for d in hardware.get('devices', [])}
    if hardware.get('node') != NODE or observed != expected:
        raise ValueError('Unexpected Red GPU identity')
    now = datetime.now(timezone.utc)
    await db.nodes.update_one({'_id': NODE}, {'$set': {
        'hostname': row['hostname'], 'os': row['os'], 'arch': row.get('arch', ''),
        'agent_version': row.get('agent_version', ''),
        'capabilities': ['inference', 'model_control', 'gpu_telemetry'],
        'machine_slug': 'machine:red', 'last_heartbeat_at': now, 'observation_source': 'restricted SSH status',
        'hardware': hardware, 'runtime': row.get('runtime', {}),
    }, '$setOnInsert': {'registered_at': now}}, upsert=True)
    return True


async def run(db):
    if not Path(SSH_CONFIG).is_file():
        return
    while True:
        try:
            await observe_once(db)
        except Exception as exc:
            logger.debug('Red observation unavailable: %s', exc)
        await asyncio.sleep(15)


async def snapshot(db):
    if db is None:
        return None
    row = await db.nodes.find_one({'_id': NODE})
    if not isinstance(row, dict):
        return None
    heartbeat = row.get('last_heartbeat_at')
    if not isinstance(heartbeat, datetime):
        return None
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - heartbeat).total_seconds() >= settings.node_heartbeat_timeout_seconds:
        return None  # An asleep host's old GPU counters are not live telemetry.
    return row


async def enrich(rows, db):
    observation = await snapshot(db)
    if observation is None:
        return rows
    for row in rows:
        if row.get('slug') != MODEL:
            continue
        hardware = observation.get('hardware', {})
        pools = hardware.get('pools', [])
        if len(pools) == 2 and all(p.get('used_gib') is not None and p.get('total_gib') is not None for p in pools):
            row.update(pool_used_gib=round(sum(p['used_gib'] for p in pools), 3),
                       pool_total_gib=round(sum(p['total_gib'] for p in pools), 3),
                       pool_spilling=any(p.get('spilling') for p in pools))
        runtime = observation.get('runtime', {})
        if row.get('state') == 'running' and runtime.get('model_id') == 'qwen3.8-27b':
            row.update(served_ctx=runtime.get('served_ctx'), ctx_per_slot=runtime.get('served_ctx'),
                       slots=runtime.get('slots'), geometry_source='red-linux running container and /v1/models')
        row['hardware_observed_at'] = hardware.get('observed_at')
    return rows
