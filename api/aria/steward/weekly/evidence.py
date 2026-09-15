"""Read-only, bounded adapters. Historical content never becomes controller input."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aria.core.logging import scrub_secrets

SECRET_KEYS = re.compile(r'(?i)(password|secret|authorization|api.?key|access.?token|refresh.?token|cookie)')
REASONING = {'reasoning', 'reasoning_content', 'reasoning_details', 'codex_reasoning_items', 'system_prompt'}
MONGO_SOURCES = {
    'usage': ('timestamp', ['source', 'model', 'session_id', 'trace_id', 'total_tokens', 'metadata']),
    'session_outcomes': ('created_at', ['session_id', 'label', 'reason', 'metrics']),
    'coding_sessions': ('updated_at', ['status', 'workspace', 'shell_name', 'review', 'error']),
    'tasks': ('updated_at', ['title', 'notes', 'status', 'project_id', 'tags']),
    'alerts': ('created_at', ['source', 'kind', 'severity', 'detail', 'dedup_key']),
    'guard_events': ('at', ['kind', 'detail', 'session_id']),
    'shell_events': ('ts', ['shell_name', 'kind', 'text_clean', 'line_number']),
}


def redact(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if SECRET_KEYS.search(str(k)) else redact(v)
                for k, v in value.items() if k not in REASONING}
    if isinstance(value, list):
        return [redact(x) for x in value[:100]]
    if isinstance(value, str):
        return scrub_secrets(value[:12000])
    return value


def entry(source, identity, timestamp, payload):
    content = redact(payload)
    encoded = json.dumps(content, sort_keys=True, default=str)
    return {'id': f'{source}:{identity}', 'source': source, 'timestamp': timestamp,
            'content': content, 'sha256': hashlib.sha256(encoded.encode()).hexdigest(),
            'redaction_version': 1}


def hermes(path, start, end, limit):
    """One read transaction sees committed WAL rows without opening the writer API."""
    uri = Path(path).expanduser().resolve(strict=True).as_uri() + '?mode=ro'
    db = sqlite3.connect(uri, uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    deadline = time.monotonic() + 15
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        cols = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
        if not {'id', 'session_id', 'role', 'content', 'timestamp'} <= cols:
            raise ValueError('Unsupported Hermes messages schema')
        selected = [c for c in ('id', 'session_id', 'role', 'content', 'timestamp', 'tool_call_id',
                               'tool_calls', 'tool_name', 'active', 'compacted', '_compressed_summary') if c in cols]
        count = db.execute('SELECT count(*) FROM messages WHERE timestamp >= ? AND timestamp < ?',
                           (start.timestamp(), end.timestamp())).fetchone()[0]
        sql = 'SELECT ' + ','.join('m.' + c for c in selected) + ',s.source,s.parent_session_id FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.timestamp >= ? AND m.timestamp < ? ORDER BY m.timestamp,m.id LIMIT ?'
        records = []
        for row in db.execute(sql, (start.timestamp(), end.timestamp(), limit)):
            data = dict(row)
            records.append(entry('hermes', data.pop('id'), data['timestamp'], data))
        return records, {'eligible': count, 'inspected': len(records), 'omitted': max(0, count-len(records)),
                         'status': 'complete' if count == len(records) else 'partial'}
    finally:
        db.close()


def log_records(path, start, end, limit, byte_limit):
    path = Path(path).expanduser()
    rows, total, skipped, bytes_read = [], 0, 0, 0
    # Current file plus rotations, bounded globally. Never follow symlinks.
    files = sorted(path.parent.glob(path.name + '*'), key=lambda p: p.stat().st_mtime)
    if not files:
        raise FileNotFoundError(str(path))
    truncated = len(files) > 10
    for file in reversed(files[-10:]):
        if file.is_symlink() or not file.is_file() or file.suffix == '.gz':
            truncated = True
            continue
        with file.open('rb') as stream:
            size = file.stat().st_size
            remaining = max(0, byte_limit - bytes_read)
            if size > remaining:
                stream.seek(size - remaining)
                stream.readline()  # partial first line
                truncated = True
            while line := stream.readline(65536):
                bytes_read += len(line)
                if bytes_read > byte_limit:
                    truncated = True
                    break
                text = line.decode(errors='replace')
                try:
                    data = json.loads(text)
                    ts = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00'))
                except (ValueError, KeyError, TypeError, AttributeError):
                    # Text logger timestamps are server local; convert explicitly.
                    try:
                        ts = datetime.strptime(text[:23], '%Y-%m-%d %H:%M:%S,%f').astimezone(timezone.utc)
                        data = {'message': text}
                    except ValueError:
                        skipped += 1
                        continue
                if ts.tzinfo is None:
                    ts = ts.astimezone(timezone.utc)
                if start <= ts < end:
                    total += 1
                    if len(rows) < limit:
                        identity = hashlib.sha256(line).hexdigest()
                        rows.append(entry('log:' + path.name, identity, ts, data))
                    else:
                        truncated = True
    return rows, {'eligible': None if truncated else total, 'inspected': len(rows),
                  'unparsed_lines': skipped, 'bytes_read': bytes_read,
                  'status': 'partial' if truncated or skipped else 'complete'}


class Collector:
    def __init__(self, db, policy):
        self.db, self.policy = db, policy

    async def collect(self, run, cursors):
        end = run['cutoff']
        sources, records, used, seen = {}, [], 0, set()
        floor = end - timedelta(days=28)

        def window(name):
            previous = cursors.get(name)
            if previous and previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            return max(floor, (previous or end-timedelta(days=7))-timedelta(minutes=5))

        def accept(name, rows, coverage):
            nonlocal used
            start = window(name)
            retained = 0
            for row in rows:
                if row['id'] in seen:
                    continue
                encoded = json.dumps(row, default=str).encode()
                if used + len(encoded) > self.policy.evidence_bytes:
                    coverage['status'] = 'partial'
                    coverage['bundle_limit'] = True
                    break
                used += len(encoded)
                seen.add(row['id'])
                records.append(row)
                retained += 1
            sources[name] = {**coverage, 'retained': retained, 'start': start, 'end': end,
                             'older_gap': bool(cursors.get(name) and cursors[name].replace(tzinfo=timezone.utc) < floor)}

        try:
            rows, meta = await asyncio.to_thread(hermes, self.policy.hermes_database, window('hermes'), end,
                                                 self.policy.records_per_source)
            accept('hermes', rows, meta)
        except Exception as exc:
            sources['hermes'] = {'status': 'unavailable', 'error': scrub_secrets(str(exc))[:500]}
        for name, (field, fields) in MONGO_SOURCES.items():
            try:
                query = {field: {'$gte': window(name), '$lt': end}}
                if name == 'shell_events':
                    query['shell_name'] = {'$not': re.compile('weekly-')}
                if name == 'tasks':
                    query['tags'] = {'$ne': 'weekly-improvement'}
                coll = self.db[name]
                sample = await coll.find_one({})
                if sample is not None and field not in sample:
                    raise ValueError('Source timestamp schema is not qualified: ' + field)
                from pymongo.errors import ExecutionTimeout
                order = [(field, 1), ('_id', 1)]
                if name == 'shell_events':
                    # Shell events are append-only ObjectIds. Bound the ingest interval
                    # through its existing index; ts still decides event eligibility.
                    from bson import ObjectId
                    query['_id'] = {'$gte': ObjectId.from_datetime(window(name))}
                    order = [('_id', -1)]
                try:
                    count = await coll.count_documents(query, maxTimeMS=5000)
                except ExecutionTimeout:
                    count = None
                raw = await coll.find(query, {field: 1, **{k: 1 for k in fields}}).sort(order).limit(self.policy.records_per_source).max_time_ms(5000).to_list(length=self.policy.records_per_source)
                rows = [entry(name, row.pop('_id'), row.get(field), row) for row in raw]
                accept(name, rows, {'eligible': count, 'inspected': len(rows), 'omitted': count-len(rows) if count is not None else None,
                                    'status': 'complete' if count == len(rows) else 'partial'})
            except Exception as exc:
                sources[name] = {'status': 'unavailable', 'error': scrub_secrets(str(exc))[:500]}
        for path in self.policy.log_files:
            name = 'log:' + Path(path).name
            try:
                rows, meta = await asyncio.to_thread(log_records, path, window(name), end,
                                                     self.policy.records_per_source, self.policy.evidence_bytes)
                accept(name, rows, meta)
            except Exception as exc:
                sources[name] = {'status': 'unavailable', 'error': scrub_secrets(str(exc))[:500]}
        return {'version': 1, 'run_id': run['_id'], 'sources': sources, 'records': records,
                'bytes': used, 'coverage': 'complete' if all(v['status']=='complete' for v in sources.values()) else 'partial'}
