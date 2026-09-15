"""Finite watched controller process. Its lifetime survives API/node restarts."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import timedelta, timezone
from pathlib import Path


async def run_controller(db, config, run_id, worker_factory=None):
    from aria.core.logging import scrub_secrets
    from aria.loop.git import TargetLock
    from aria.steward.weekly.codex import Worker
    from aria.steward.weekly.policy import load_policy, digest
    from aria.steward.weekly.store import Store, now
    from aria.steward.weekly.evidence import Collector
    from aria.steward.weekly.execution import Workspace, TrackedRuntime, merge_candidate
    from aria.steward.weekly.deployment import Deployer
    from aria.steward.weekly.reporting import render, publish, deliver

    root = Path(config.state_dir).expanduser().resolve()
    lock = TargetLock(root/'locks', 'weekly-platform').acquire()
    store, workspaces = Store(db), []
    run = None
    try:
        run = await store.claim(run_id)
        if not run:
            return
        policy, fingerprint = load_policy(config)
        if fingerprint != run['policy_hash']:
            raise ValueError('Policy changed since admission')
        run['deadline'] = run['deadline'].replace(tzinfo=timezone.utc)
        run['cutoff'] = run['cutoff'].replace(tzinfo=timezone.utc)
        run_root = root/'runs'/run_id
        run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A prior owner may have left stopped/expiring containers. Reconcile them
        # before starting any fresh stage; local flock proves no live controller.
        from aria.loop.runtime import ContainerRuntime
        runtime = TrackedRuntime(db, run_id, policy.docker_binary, state_root=run_root)
        for container in await db.improvement_containers.find({'run_id': run_id, 'active': True}).to_list(length=100):
            await runtime.stop(container['_id'])
            await db.improvement_containers.update_one({'_id': container['_id']}, {'$set': {'active': False}})
        if run.get('phase') not in {'admitted', 'collect', 'review'}:
            run['findings'] = await db.improvement_findings.find({'run_id': run_id}).to_list(length=30)
            for finding in run['findings']:
                target = policy.targets.get(finding['target'])
                if target and target.deployment and finding.get('deployment_intent'):
                    reconciled = await Deployer(root/'deployments'/finding['_id'], finding['target'], target).call('reconcile', finding['deployment_intent'])
                    finding['reconciliation'] = reconciled
                    if reconciled['status'] == 'active':
                        finding['deployment'] = {**finding['deployment_intent'], **reconciled}
                        finding['disposition'] = 'deployed'
                        finding['watch'] = {'active': True, 'until': now()+timedelta(hours=target.deployment.watch_hours)}
                    elif reconciled['status'] == 'rolled_back':
                        finding['disposition'] = 'rolled_back'
                    await db.improvement_findings.update_one({'_id': finding['_id']}, {'$set': {k:v for k,v in finding.items() if k!='_id'}})
            raise RuntimeError('Interrupted mutation phase reconciled; retained candidates are not automatically replayed')

        async def checkpoint():
            await store.check(run)
            if now() >= run['deadline']-timedelta(seconds=600):
                raise TimeoutError('Review deadline reached; preserving time for reporting')
            _, current = load_policy(config)
            if current != fingerprint:
                raise RuntimeError('Operator policy changed')
            # Read the same persistent stop records as the API managers.
            from aria.api.deps import get_killswitch, resolve_estop_manager
            manual_stop = await db.killswitch.find_one({'_id': 'global'})
            if manual_stop and manual_stop.get('active'):
                raise RuntimeError('Manual killswitch is active')
            get_killswitch().check_or_raise('weekly improvement')
            if await (await resolve_estop_manager(db)).is_active():
                raise RuntimeError('Emergency stop active')
            await store.save(run)

        async def log(kind, payload):
            text = scrub_secrets(json.dumps({'kind': kind, **payload}, default=str))
            print(text[:14000], flush=True)
            if kind == 'usage':
                if payload['tokens'] is None:
                    run['usage']['unknown_calls'] += 1
                else:
                    run['usage']['tokens'] += payload['tokens']
                await store.save(run, usage=run['usage'])
            await db.improvement_events.insert_one({'run_id': run_id, 'kind': kind, 'at': now(), 'content': text[:14000]})

        await store.save(run, phase='collect')
        cursor = await db.improvement_state.find_one({'_id': 'cursors'}) or {}
        evidence_path = run_root/'evidence.json'
        bundle = json.loads(evidence_path.read_text()) if evidence_path.exists() else await Collector(db, policy).collect(run, cursor.get('sources', {}))
        (run_root/'evidence.json').write_text(json.dumps(bundle, default=str))
        (run_root/'evidence.json').chmod(0o600)
        await store.save(run, sources=bundle['sources'], coverage=bundle['coverage'])
        updated_cursors = dict(cursor.get('sources', {}))
        for key, source in bundle['sources'].items():
            if source['status'] == 'complete':
                updated_cursors[key] = run['cutoff']
        await db.improvement_state.update_one({'_id': 'cursors'}, {'$set': {'sources': updated_cursors}}, upsert=True)
        review_spaces = {}
        for key, target in policy.targets.items():
            await checkpoint()
            workspace = Workspace(run_root/key/('review-'+str(run['generation'])), key, target, run_id, 'review'+str(run['generation']), runtime=runtime)
            await db.improvement_containers.update_one({'_id': workspace.container}, {'$set': {'run_id': run_id, 'active': True}}, upsert=True)
            workspaces.append(workspace)
            try:
                await workspace.prepare(readonly=True, seconds=max(1, int((run['deadline']-now()).total_seconds())), evidence=bundle)
                review_spaces[key] = workspace
            except Exception as exc:
                run['sources']['source:'+key] = {'status': 'unavailable', 'error': scrub_secrets(str(exc))[:500]}
                run['coverage'] = 'partial'
        async def inspect(target, command):
            await checkpoint()
            if target not in review_spaces:
                return {'exit_code': 1, 'output': 'Target source is unavailable; record a report-only recommendation.'}
            return await review_spaces[target].command(command)
        prior = await db.improvement_findings.find({}, {'title': 1, 'target': 1, 'disposition': 1, 'next_step': 1}).sort('last_seen', -1).limit(100).to_list(length=100)
        samples = []
        for source in bundle['sources']:
            rows = [row for row in bundle['records'] if row['source'] == source]
            selected = rows[:3] + rows[-3:] if len(rows)>6 else rows
            samples.extend(selected)
        prompt = {'mode': 'review_only', 'evidence': {**bundle, 'records': samples}, 'prior_findings': prior,
                  'evidence_file': '/workspace/__aria_review_evidence.json',
                  'instruction': 'Initial excerpts sample every source. Query the read-only evidence file with bounded commands for missing context; do not claim exhaustive review.',
                  'targets': {key: {'revision': value.base, 'allowed_paths': value.policy.allowed_paths,
                                    'acceptance_checks': value.policy.acceptance_check_ids,
                                    'acceptance_contracts': value.policy.check_descriptions} for key, value in review_spaces.items()}}
        # Evidence may be larger than the model context: explicit bounded selection.
        while len(json.dumps(prompt, default=str)) > policy.context_chars//2 and samples:
            samples.pop()
        if len(samples) < len(bundle['records']):
            run['coverage'] = 'partial'
        await store.save(run, phase='review', coverage=run['coverage'], model_records=len(samples))
        worker = (worker_factory or Worker)(policy)
        result = await asyncio.wait_for(worker.run(prompt, inspect, checkpoint, log),
                                       timeout=max(1, (run['deadline']-now()).total_seconds()-600))
        await store.save(run, summary=result.summary)
        evidence_ids = {row['id'] for row in bundle['records']}
        changes = 0
        for item in result.findings:
            await checkpoint()
            finding = item.model_dump()
            identity = item.fingerprint()
            prior = await db.improvement_findings.find_one({'fingerprint': identity})
            finding.update(_id=identity, fingerprint=identity, last_seen=now(), run_id=run_id, policy_hash=fingerprint)
            target = policy.targets.get(item.target)
            finding['disposition'] = 'suggested'
            reason = None
            if prior and prior.get('disposition') in {'deployed', 'dismissed', 'deferred', 'active', 'verified', 'merged'}:
                reason = 'Existing finding '+identity+' retained: '+prior['disposition']
            elif item.confidence != 'high':
                reason = 'Confidence requires further evidence'
            elif not item.evidence_ids or not set(item.evidence_ids) <= evidence_ids:
                reason = 'Missing or invalid evidence references'
            elif not target or item.check_id not in target.acceptance_check_ids:
                reason = 'No registered behavioral acceptance check for this target'
            elif not target.deployment:
                reason = 'No qualified deployment adapter'
            elif run['report_only']:
                reason = 'Report-only qualification run'
            elif changes >= policy.max_changes:
                reason = 'Weekly change limit reached'
            if not prior:
                await db.improvement_findings.insert_one({**finding, 'first_seen': now()})
            else:
                await db.improvement_findings.update_one({'_id': identity}, {'$set': {'last_seen': now()}})
            if reason:
                finding['reason'] = reason
            else:
                changes += 1
                await store.save(run, phase='implement')
                from bson import ObjectId
                from aria.planning.service import _content_hash
                task_id = ObjectId(identity[:24])
                await db.tasks.update_one({'_id': task_id}, {'$setOnInsert': {'title': item.title, 'notes': item.problem,
                    'status': 'active', 'owner': 'agent', 'tags': ['weekly-improvement'],
                    'source': {'type': 'awareness', 'shell_name': run.get('shell_name')}, 'content_hash': _content_hash(item.title),
                    'created_at': now(), 'updated_at': now()}}, upsert=True)
                finding['task_id'] = str(task_id)
                await db.improvement_findings.update_one({'_id': identity}, {'$set': {'disposition': 'active', 'task_id': str(task_id)}})
                workspace = Workspace(run_root/item.target/identity[:12], item.target, target, run_id, identity[:12], runtime=runtime)
                workspaces.append(workspace)
                deployer = Deployer(root/'deployments'/identity, item.target, target)
                try:
                    await checkpoint()
                    await db.improvement_containers.update_one({'_id': workspace.container}, {'$set': {'run_id': run_id, 'active': True}}, upsert=True)
                    await workspace.prepare(seconds=max(1, int((run['deadline']-now()).total_seconds())))
                    async def implement(selected, command):
                        await checkpoint()
                        if selected != item.target:
                            raise ValueError('Implementation attempted another target')
                        return await workspace.command(command)
                    await asyncio.wait_for(worker.run({'mode': 'implement', 'finding': item.model_dump(),
                        'target': item.target, 'allowed_paths': target.allowed_paths, 'instruction': 'Implement only this finding. Finish with an empty findings list.'}, implement, checkpoint, log),
                        timeout=max(1, (run['deadline']-now()).total_seconds()-600))
                    await store.save(run, phase='verify')
                    finding['verification'] = await asyncio.wait_for(workspace.verify(item.check_id), timeout=max(1, (run['deadline']-now()).total_seconds()-600))
                    finding['disposition'] = 'verified'
                    await checkpoint()
                    # Reserve an adapter's full timeout plus recovery before merging.
                    if (run['deadline']-now()).total_seconds() < 2*target.deployment.command.timeout_seconds+600:
                        raise TimeoutError('Insufficient time for deployment and rollback')
                    before_deploy = await deployer.call('preflight', {})
                    await checkpoint()
                    await store.save(run, phase='merge')
                    candidate = await merge_candidate(target, finding['verification'], root/'locks', identity)
                    finding['disposition'] = 'merged'
                    request = {'run_id': run_id, 'finding_id': identity, 'candidate': candidate,
                               'repository': target.repository, 'base_revision': workspace.base, 'expected_identity': before_deploy.get('identity')}
                    finding['deployment_intent'] = request
                    await db.improvement_findings.update_one({'_id': identity}, {'$set': {k:v for k,v in finding.items() if k!='_id'}})
                    await checkpoint()
                    await store.save(run, phase='deploy')
                    deployed = await deployer.call('apply', request)
                    finding['deployment'] = {**request, **deployed}
                    await db.improvement_findings.update_one({'_id': identity}, {'$set': {'deployment': finding['deployment']}})
                    health = await deployer.call('health', finding['deployment'])
                    if not health.get('healthy'):
                        finding['rollback'] = await deployer.call('rollback', finding['deployment'])
                        finding['disposition'] = 'rolled_back'
                    else:
                        finding['disposition'] = 'deployed'
                        finding['watch'] = {'active': True, 'until': now()+timedelta(hours=target.deployment.watch_hours)}
                        await db.tasks.update_one({'_id': task_id}, {'$set': {'status': 'done', 'completed_at': now()}})
                except Exception as exc:
                    if not finding.get('deployment') and finding.get('deployment_intent'):
                        try:
                            recovered = await deployer.call('reconcile', finding['deployment_intent'])
                            finding['reconciliation'] = recovered
                            if recovered['status'] == 'active':
                                finding['deployment'] = {**finding['deployment_intent'], **recovered}
                            elif recovered['status'] == 'rolled_back':
                                finding['disposition'] = 'rolled_back'
                        except Exception as recovery_error:
                            finding['recovery_error'] = scrub_secrets(str(recovery_error))[:1000]
                    if finding.get('deployment') and finding['disposition'] != 'rolled_back':
                        try:
                            finding['rollback'] = await deployer.call('rollback', finding['deployment'])
                            finding['disposition'] = 'rolled_back'
                        except Exception as rollback_error:
                            finding['rollback_error'] = scrub_secrets(str(rollback_error))[:1000]
                    finding['reason'] = scrub_secrets(str(exc))[:2000]
                    if finding['disposition'] not in {'verified', 'merged', 'rolled_back'}:
                        finding['disposition'] = 'unverified'
                await db.improvement_findings.update_one({'_id': identity}, {'$set': {k:v for k,v in finding.items() if k!='_id'}})
            run['findings'].append(finding)
            await store.save(run, findings=run['findings'])
        run['status'] = 'completed' if run['coverage']=='complete' else 'partial'
    except BaseException as exc:
        if run:
            run['status'] = 'interrupted' if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else 'failed'
            run['error'] = scrub_secrets(str(exc))[:2000]
    finally:
        for workspace in reversed(workspaces):
            try:
                await workspace.stop()
                await db.improvement_containers.update_one({'_id': workspace.container}, {'$set': {'active': False}})
            except Exception as exc:
                if run:
                    run['status'], run['error'] = 'failed', 'Container cleanup failed: '+str(exc)[:1000]
        if run:
            run['report'] = render(run)
            # Private local recovery copy exists even if Mongo is temporarily unavailable.
            (root/(run_id+'-report.md')).write_text(run['report'])
            try:
                await store.finish(run, run['status'], report=run['report'], error=run.get('error'), findings=run['findings'])
                await publish(db, run)
                await deliver(db, run)
            except Exception as exc:
                print('Report persistence/delivery pending: '+scrub_secrets(str(exc))[:500], flush=True)
        lock.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('run_id')
    parser.add_argument('--policy', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--environment', required=True)
    args = parser.parse_args()
    # Import settings only after loading the controller's private config.
    environment = json.loads(Path(args.environment).read_text())
    for key, value in environment.items():
        os.environ[key] = json.dumps(value) if isinstance(value, bool) else str(value)
    from motor.motor_asyncio import AsyncIOMotorClient
    from aria.config import settings
    from aria.steward.weekly.policy import WeeklySettings
    async def start():
        import signal
        loop = asyncio.get_running_loop()
        current_task = asyncio.current_task()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, current_task.cancel)
        client = AsyncIOMotorClient(settings.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=5000, socketTimeoutMS=10000)
        try:
            await run_controller(client[settings.mongodb_database], WeeklySettings(enabled=True, policy_file=args.policy, state_dir=args.state), args.run_id)
        finally:
            client.close()
    asyncio.run(start())


if __name__ == '__main__':
    main()
