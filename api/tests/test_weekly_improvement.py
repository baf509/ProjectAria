"""Weekly review contracts, using disposable data and trusted fixture processes."""
import asyncio
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mongomock_motor import AsyncMongoMockClient

from aria.scheduler.service import SchedulerService
from aria.steward.weekly.evidence import Collector, hermes, log_records, redact
from aria.steward.weekly.policy import Finding, Policy, Target, WeeklySettings, load_policy
from aria.steward.weekly.store import LostLease, Store
from aria.steward.weekly.execution import Workspace, merge_candidate, validate_changes
from aria.steward.weekly.reporting import publish
from aria.loop.git import git
from .test_loop import TrustedProcessRuntime


@pytest.fixture
def db():
    return AsyncMongoMockClient(tz_aware=True)['weekly_test']


@pytest.fixture
def sqlite_file(tmp_path):
    p = tmp_path/'state.db'
    connection = sqlite3.connect(p)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, started_at REAL)')
    connection.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL, reasoning TEXT, active INTEGER, compacted INTEGER)')
    connection.execute("INSERT INTO sessions VALUES ('old-session','signal',NULL,0)")
    connection.commit()
    yield p, connection
    connection.close()


def test_hermes_reads_committed_wal_and_long_lived_sessions(sqlite_file):
    p, connection = sqlite_file
    now = datetime.now(timezone.utc)
    connection.execute('INSERT INTO messages VALUES (1,?,?,?,?,?,?,?)', ('old-session','user','Fix the repeated failure',now.timestamp(),'private reasoning',1,0))
    connection.commit()
    before = p.read_bytes()
    rows, coverage = hermes(p, now-timedelta(days=7), now+timedelta(seconds=1), 10)
    assert len(rows) == 1 and coverage['status'] == 'complete'
    assert rows[0]['content']['session_id'] == 'old-session'
    assert 'reasoning' not in rows[0]['content']
    assert before == p.read_bytes()


def test_hermes_reports_truncation(sqlite_file):
    p, connection = sqlite_file
    now = datetime.now(timezone.utc)
    for i in range(4):
        connection.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)', (i,'old-session','user','hi',now.timestamp(),None,1,0))
    connection.commit()
    rows, coverage = hermes(p, now-timedelta(days=1), now+timedelta(seconds=1), 2)
    assert len(rows) == 2 and coverage['omitted'] == 2 and coverage['status'] == 'partial'


def test_recursive_redaction():
    value = redact({'tool_calls': [{'arguments': {'api_key':'secret-value', 'message':'Bearer abcdefgh123456789'}}], 'reasoning_content':'hidden'})
    assert 'secret-value' not in str(value) and 'abcdefgh' not in str(value) and 'hidden' not in str(value)


def test_rotated_log_and_unparsed_coverage(tmp_path):
    now = datetime.now(timezone.utc)
    p = tmp_path/'aria.log'
    p.write_text(json.dumps({'timestamp':now.isoformat(),'message':'failed'})+'\nstack continuation\n')
    rows, meta = log_records(p, now-timedelta(hours=1), now+timedelta(hours=1), 5, 4096)
    assert len(rows)==1 and meta['status']=='partial' and meta['unparsed_lines']==1


async def test_admission_deduplicates_occurrences_and_overlap(db):
    store = Store(db)
    await store.initialize()
    runs = await asyncio.gather(*(store.admit('platform','hash',occurrence='thursday') for _ in range(5)))
    assert len({r['_id'] for r in runs}) == 1
    active = await store.admit('platform','hash',occurrence='manual')
    assert active['_id'] == runs[0]['_id']
    claimed = await store.claim(active['_id'])
    await store.finish(claimed,'completed')
    same = await store.admit('platform','hash',occurrence='thursday')
    assert same['_id']==active['_id']
    later = await store.admit('platform','hash',occurrence='next-thursday')
    assert later['_id']!=active['_id']


async def test_stale_generation_and_cancel_fail_closed(db):
    store = Store(db)
    await store.initialize()
    run = await store.admit('platform','hash',occurrence='thursday')
    first = await store.claim(run['_id'])
    second = await store.claim(run['_id'])
    with pytest.raises(LostLease):
        await store.save(first,phase='deploy')
    await db.improver_runs.update_one({'_id':run['_id']},{'$set':{'cancel_requested':True}})
    with pytest.raises(LostLease):
        await store.check(second)


@pytest.mark.parametrize('anchor,expected', [('2026-03-06T10:00:00+00:00','2026-03-12T08:00:00+00:00'), ('2026-10-30T10:00:00+00:00','2026-11-05T09:00:00+00:00')])
def test_weekly_timezone_across_dst(monkeypatch, anchor, expected):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(anchor).astimezone(tz)
    monkeypatch.setattr('aria.scheduler.service.datetime', Clock)
    assert SchedulerService._compute_next_run('weekly thu 04:00',timezone_name='America/New_York').isoformat()==expected


def test_invalid_timezone():
    with pytest.raises(ValueError):
        SchedulerService._compute_next_run('weekly thu 04:00',timezone_name='not/a-zone')


async def fixture_target(tmp_path):
    repo = tmp_path/'repo';repo.mkdir()
    await git('-C',repo,'init','-b','main')
    (repo/'answer.txt').write_text('wrong')
    await git('-C',repo,'add','.')
    await git('-C',repo,'commit','-m','baseline')
    checks = tmp_path/'checks';checks.mkdir()
    (checks/'check.py').write_text("from pathlib import Path\nassert Path('answer.txt').read_text() == 'right'\n")
    target = Target(repository=str(repo), assets=str(checks), branch='main',allowed_paths=['answer.txt'],
        image='sha256:'+'a'*64,checks={'answer':{'argv':[sys.executable,'/checks/check.py'],'version':'1'}},acceptance_check_ids=['answer'])
    return target


async def test_independent_baseline_candidate_verification_and_merge(tmp_path):
    target = await fixture_target(tmp_path)
    runtime = TrustedProcessRuntime()
    w = Workspace(tmp_path/'work','aria',target,'run','attempt',runtime=runtime)
    await w.prepare()
    (w.path/'answer.txt').write_text('right')
    # TrustedProcessRuntime fixture needs cwd because the check reads a relative path.
    target.checks['answer'].argv = [sys.executable,'-c',"from pathlib import Path; assert Path('/workspace/answer.txt').read_text() == 'right'"]
    evidence = await w.verify('answer')
    assert evidence['before']['answer']['exit_code'] != 0
    assert evidence['after']['answer']['exit_code'] == 0
    await merge_candidate(target,evidence,tmp_path/'locks','one')
    assert (Path(target.repository)/'answer.txt').read_text()=='right'


async def test_dirty_destination_and_protected_targets_refused(tmp_path):
    target = await fixture_target(tmp_path)
    (Path(target.repository)/'unrelated.txt').write_text('human work')
    with pytest.raises(ValueError, match='dirty'):
        base=(await git('-C',target.repository,'rev-parse','HEAD')).decode().strip()
        await merge_candidate(target,{'base':base,'candidate':base},tmp_path/'locks','one')
    target.allowed_paths=['api/']
    with pytest.raises(ValueError, match='scope'):
        validate_changes(['api/aria/steward/weekly/policy.py'],target)
    with pytest.raises(ValueError, match='scope'):
        validate_changes(['api/tests/test_api.py'],target)


async def test_report_retries_use_same_path_and_preserve_human_edits(db,tmp_path,monkeypatch):
    from aria.config import settings
    monkeypatch.setattr(settings,'obsidian_enabled',True)
    monkeypatch.setattr(settings,'obsidian_vault_path',str(tmp_path))
    run={'_id':'one','status':'completed','findings':[]}
    await db.improver_runs.insert_one(dict(run))
    first=await publish(db,run)
    second=await publish(db,run)
    assert first['path']==second['path']
    p=Path(first['path']);p.write_text(p.read_text()+'\nBen edited this.\n')
    third=await publish(db,run)
    assert third['status']=='failed' and 'Ben edited this.' in p.read_text()


def test_restart_helper_rejects_arbitrary_commands():
    import subprocess
    helper=Path(__file__).resolve().parents[2]/'scripts/aria-service-restart-helper'
    for args in [('restart','unknown'),('exec','aria-api'),('restart','aria-api','extra')]:
        result=subprocess.run(['/bin/sh',str(helper),*args],capture_output=True)
        assert result.returncode==64


def test_config_adapter_only_changes_allowlisted_typed_fields():
    import importlib.machinery
    import importlib.util
    path=Path(__file__).resolve().parents[2]/'scripts/aria-weekly-deploy-adapter'
    loader=importlib.machinery.SourceFileLoader('weekly_adapter_test',str(path))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    module=importlib.util.module_from_spec(spec);loader.exec_module(module)
    rules={'context':{'type':'int','min':4096,'max':32768}}
    assert module.field_updates({'context':8192},{'context':16384},rules)=={'context':(8192,16384)}
    for after in ({'context':True},{'context':999999},{'context':8192,'approval':'approved'}):
        with pytest.raises(ValueError):
            module.field_updates({'context':8192},after,rules)


async def test_new_api_read_and_admin_trigger_boundary(db):
    import httpx
    from fastapi import FastAPI
    from aria.api.routes.improve import router
    from aria.api.deps import get_db
    app=FastAPI();app.include_router(router,prefix='/api/v1')
    app.dependency_overrides[get_db]=lambda:db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get('/api/v1/improve/runs')).json()==[]
        response=await client.post('/api/v1/improve/trigger',json={})
        assert response.status_code in (401,403,503)
        assert (await client.get('/api/v1/improve/runs/absent')).status_code==404


def test_configuration_deploy_rollback_and_interrupted_reconciliation(tmp_path):
    """Real adapter subprocesses preserve unrelated values and reject later edits."""
    import hashlib
    import subprocess
    repo=tmp_path/'repo';repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL,text=True).strip()
    git('init','-b','main')
    source=repo/'settings.json';source.write_text('{"context":8192}')
    git('add','.');git('-c','user.name=Test','-c','user.email=test@local','commit','-m','before')
    before=git('rev-parse','HEAD')
    source.write_text('{"context":16384}')
    git('add','.');git('-c','user.name=Test','-c','user.email=test@local','commit','-m','after')
    after=git('rev-parse','HEAD')
    live=tmp_path/'live.json';live.write_text('{"context":8192,"unrelated":"keep"}')
    health=tmp_path/'health';health.write_text('#!/bin/sh\nprintf \'{"healthy":true,"samples":12}\\n\'\n');health.chmod(0o700)
    registry=tmp_path/'registry.json';registry.write_text(json.dumps({'state_dir':str(tmp_path/'receipts'),'targets':{'config':{
        'kind':'json_fields','repository':str(repo),'source_file':'settings.json','destinations':[str(live)],
        'fields':{'context':{'type':'int','min':4096,'max':32768}},
        'health_command':{'executable':str(health),'sha256':hashlib.sha256(health.read_bytes()).hexdigest()}}}}))
    adapter=Path(__file__).resolve().parents[2]/'scripts/aria-weekly-deploy-adapter'
    def call(operation,request):
        file=tmp_path/'request.json';file.write_text(json.dumps({'target_id':'config',**request}))
        result=subprocess.run([str(adapter),operation,str(file),str(registry)],capture_output=True,text=True)
        if result.returncode:raise RuntimeError(result.stdout+result.stderr)
        return json.loads(result.stdout.splitlines()[-1])
    preflight=call('preflight',{})
    request={'finding_id':'abcd1234','repository':str(repo),'base_revision':before,'candidate':after,'expected_identity':preflight['identity']}
    applied=call('apply',request)
    assert json.loads(live.read_text())=={'context':16384,'unrelated':'keep'}
    assert call('apply',request)['identity']==applied['identity']
    assert call('reconcile',request)['status']=='active'
    receipt_path=Path(applied['receipt']);receipt=json.loads(receipt_path.read_text())
    # Simulate death after writing live config but before recording active.
    receipt['status']='activating';receipt_path.write_text(json.dumps(receipt))
    assert call('reconcile',request)['status']=='rolled_back'
    assert json.loads(live.read_text())=={'context':8192,'unrelated':'keep'}
    request['finding_id']='abcd5678'
    applied=call('apply',request)
    live.write_text('{"context":24576,"unrelated":"human edit"}')
    with pytest.raises(RuntimeError,match='another version owns'):
        call('rollback',request)
    assert json.loads(live.read_text())['unrelated']=='human edit'
    live.write_text('{"context":16384,"unrelated":"keep"}\n')
    # Use original deployed bytes, since ownership intentionally includes formatting.
    live.write_text(json.dumps({'context':16384,'unrelated':'keep'},indent=2)+'\n')
    assert call('rollback',request)['status']=='rolled_back'
    assert json.loads(live.read_text())['context']==8192


async def test_schedule_cannot_bypass_weekly_admin_gate(db):
    import httpx
    from fastapi import FastAPI
    from aria.api.routes.schedules import router
    from aria.api.deps import get_scheduler
    from aria.scheduler.service import SchedulerService
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_scheduler]=lambda:SchedulerService(db, None, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        result=await client.post('/schedules',json={'name':'bypass','action':'improvement','schedule_type':'recurring','cron_expr':'every 1m'})
        assert result.status_code in (403,503)
        assert await db.schedules.count_documents({})==0


async def test_recovery_does_not_mistake_idle_tmux_for_live_controller(db,tmp_path):
    from aria.steward.weekly.service import WeeklyImprovement
    service=WeeklyImprovement(db,config=WeeklySettings(state_dir=str(tmp_path)))
    await service.store.initialize()
    run=await service.store.admit('platform','hash',occurrence='recovery')
    await db.improver_runs.update_one({'_id':run['_id']},{'$set':{'launch_attempt':1,'launched_at':datetime.now(timezone.utc)-timedelta(minutes=2)}})
    shell=SimpleNamespace(name='replacement')
    shells=SimpleNamespace(tmux=SimpleNamespace(has_session=AsyncMock(return_value=True)),
        create_shell=AsyncMock(return_value=shell),shells=db.shells)
    service.shells=shells
    await service.launch(run)
    assert shells.create_shell.call_args.args[0].endswith('-r2')
    assert (await db.improver_runs.find_one({'_id':run['_id']}))['launch_attempt']==2


async def test_verified_candidate_survives_missing_deployment_privilege(db,tmp_path,monkeypatch):
    from aria.steward.weekly import execution, deployment, reporting
    from aria.steward.weekly.runner import run_controller
    from aria.steward.weekly.policy import Step
    from aria.planning.service import PlanningService
    from aria.config import settings
    target=await fixture_target(tmp_path)
    target.checks['answer'].argv=[sys.executable,'-c',"from pathlib import Path; assert Path('/workspace/answer.txt').read_text() == 'right'"]
    adapter=tmp_path/'adapter';adapter.write_text('trusted adapter')
    spec=target.model_dump();spec['deployment']={'command':{'executable':str(adapter),'sha256':hashlib.sha256(adapter.read_bytes()).hexdigest()}}
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'hermes_database':str(tmp_path/'absent.db'),'targets':{'aria':spec}}))
    config=WeeklySettings(policy_file=str(policy),state_dir=str(tmp_path/'state'))
    _,fingerprint=load_policy(config)
    runtime=TrustedProcessRuntime();monkeypatch.setattr(execution,'TrackedRuntime',lambda *a,**kw:runtime)
    monkeypatch.setattr(reporting,'publish',AsyncMock());monkeypatch.setattr(reporting,'deliver',AsyncMock())
    monkeypatch.setattr(deployment.Deployer,'call',AsyncMock(side_effect=RuntimeError('Restart privilege unavailable')))
    monkeypatch.setattr(settings,'obsidian_enabled',False)
    async def collect(*a):
        return {'sources':{'hermes':{'status':'complete'}},'records':[{'source':'hermes','id':'hermes:1','payload':'Known wrong answer'}],'coverage':'complete'}
    monkeypatch.setattr(Collector,'collect',collect)
    class Worker:
        def __init__(self,policy):pass
        async def run(self,prompt,execute,checkpoint,log):
            if prompt['mode']=='implement':
                await execute('aria',"printf right > /workspace/answer.txt")
                return Step(action='finish',target='',command='',findings=[],summary='implemented')
            return Step(action='finish',target='',command='',summary='review',findings=[Finding(
                title='Wrong answer',target='aria',problem='Answer is wrong',expected_behavior='right',proposed_fix='Set right',confidence='high',rationale='fixture',contrary_evidence='',evidence_ids=['hermes:1'],check_id='answer',next_step='verify')])
    store=Store(db);await store.initialize()
    run=await store.admit('platform',fingerprint,occurrence='retained')
    await run_controller(db,config,run['_id'],worker_factory=Worker)
    result=await db.improver_runs.find_one({'_id':run['_id']})
    assert result['findings'][0]['disposition']=='verified',result
    assert 'privilege' in result['findings'][0]['reason']
    assert (Path(target.repository)/'answer.txt').read_text()=='wrong'
    tasks=await PlanningService(db).list_tasks()
    assert len(tasks)==1 and tasks[0].owner=='agent'


async def test_codex_reserves_a_final_report_before_context_limit(tmp_path):
    from aria.steward.weekly.codex import Worker
    target=await fixture_target(tmp_path)
    policy=Policy(hermes_database='/absent',targets={'aria':target},context_chars=4000,max_turns=2)
    class Connection:
        def __init__(self,*a,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def start(self,*a):return 'thread'
        async def turn(self,thread,text,output_schema):
            assert output_schema['properties']['action']['enum']==['finish']
            assert 'Finish now' in text
            return json.dumps({'action':'finish','target':'','command':'','findings':[],'summary':'Partial review retained'}),10
    execute=AsyncMock()
    result=await Worker(policy,connection=Connection).run({'mode':'review_only'},execute,AsyncMock(),AsyncMock())
    assert result.summary=='Partial review retained'
    execute.assert_not_called()
