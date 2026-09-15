"""Durable occurrence admission and fenced updates; flock prevents live-owner takeover."""
from __future__ import annotations

import uuid
import os
import psutil
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError


def now():
    return datetime.now(timezone.utc)


class LostLease(RuntimeError):
    pass


class Store:
    def __init__(self, db):
        self.db = db
        self.runs = db.improver_runs

    async def initialize(self):
        await self.runs.create_index('occurrence', unique=True, sparse=True)
        await self.runs.create_index('active_slot', unique=True, sparse=True)
        await self.runs.create_index([('schema_version', 1), ('created_at', -1)])
        await self.db.improvement_findings.create_index('fingerprint', unique=True)

    async def admit(self, policy_id, policy_hash, *, occurrence, report_only=False, seconds=7200):
        existing = await self.runs.find_one({'occurrence': occurrence})
        if existing:
            return existing
        active = await self.runs.find_one({'active_slot': 'platform'})
        if active:
            return active
        current = now()
        run = {'_id': uuid.uuid4().hex, 'schema_version': 2, 'policy_id': policy_id,
               'policy_hash': policy_hash, 'occurrence': occurrence, 'active_slot': 'platform',
               'created_at': current, 'cutoff': current, 'deadline': current+timedelta(seconds=seconds),
               'status': 'queued', 'phase': 'admitted', 'report_only': report_only, 'generation': 0,
               'cancel_requested': False, 'events': [], 'findings': [], 'usage': {'tokens': 0, 'unknown_calls': 0},
               'publication': {'status': 'pending'}, 'notification': {'status': 'pending'}}
        try:
            await self.runs.insert_one(run)
            return run
        except DuplicateKeyError:
            return await self.runs.find_one({'occurrence': occurrence}) or await self.runs.find_one({'active_slot': 'platform'})

    async def claim(self, run_id):
        # Caller MUST own the physical local flock before claiming/reclaiming.
        return await self.runs.find_one_and_update(
            {'_id': run_id, 'active_slot': 'platform'},
            {'$inc': {'generation': 1}, '$set': {'heartbeat': now(), 'lease_expires_at': now()+timedelta(seconds=45), 'status': 'running', 'owner_pid': os.getpid(), 'owner_started': psutil.Process().create_time()}},
            return_document=ReturnDocument.AFTER)

    async def save(self, run, **fields):
        result = await self.runs.update_one({'_id': run['_id'], 'generation': run['generation'], 'active_slot': 'platform'},
                                           {'$set': {**fields, 'heartbeat': now(), 'lease_expires_at': now()+timedelta(seconds=45)}})
        if result.matched_count != 1:
            raise LostLease('Run ownership changed')
        run.update(fields)

    async def check(self, run):
        doc = await self.runs.find_one({'_id': run['_id'], 'generation': run['generation'], 'active_slot': 'platform'})
        if not doc or doc.get('cancel_requested'):
            raise LostLease('Run cancelled or ownership changed')
        return doc

    async def finish(self, run, status, **fields):
        result = await self.runs.update_one({'_id': run['_id'], 'generation': run['generation']},
            {'$set': {**fields, 'status': status, 'phase': 'finished', 'finished_at': now()}, '$unset': {'active_slot': ''}})
        if result.matched_count != 1:
            raise LostLease('Terminal run ownership changed')
        run.update(fields, status=status)
