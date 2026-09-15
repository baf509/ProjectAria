"""API lifecycle and scheduler admission. Expensive work lives in watched controller shells."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

from aria.config import settings
from aria.steward.weekly.policy import WeeklySettings, load_policy
from aria.steward.weekly.store import Store, now


class WeeklyImprovement:
    def __init__(self, db, shells=None, config=None):
        self.db, self.shells = db, shells
        self.config = config or WeeklySettings()
        self.store = Store(db)

    async def initialize(self):
        await self.store.initialize()
        if not self.config.enabled:
            return
        policy, _ = load_policy(self.config)
        from aria.scheduler.service import SchedulerService
        for identity, expression, action in [('weekly-platform-improvement', 'weekly thu 04:00', 'improvement'),
                                               ('weekly-platform-watch', 'every 15m', 'improvement_watch')]:
            await self.db.schedules.update_one({'_id': identity}, {'$setOnInsert': {
                'name': identity, 'schedule_type': 'recurring', 'action': action,
                'params': {'policy_id': policy.id}, 'cron_expr': expression, 'timezone': 'America/New_York',
                'enabled': True, 'next_run_at': SchedulerService._compute_next_run(expression, timezone_name='America/New_York'),
                'created_at': now(), 'run_count': 0}}, upsert=True)
        # Restart recovery reconciles deterministic shell identities, never blindly duplicates a process.
        active = await self.db.improver_runs.find_one({'active_slot': 'platform'})
        if active:
            await self.launch(active)

    async def trigger(self, policy_id='platform', occurrence=None, report_only=False):
        if not self.config.enabled:
            raise ValueError('Weekly improvement is disabled')
        from aria.api.deps import get_killswitch, resolve_estop_manager
        get_killswitch().check_or_raise('weekly improvement')
        if await (await resolve_estop_manager(self.db)).is_active():
            raise ValueError('Emergency stop is active')
        policy, fingerprint = load_policy(self.config)
        if policy_id != policy.id:
            raise ValueError('Unknown weekly policy ID')
        run = await self.store.admit(policy.id, fingerprint, occurrence=occurrence or 'manual:'+now().isoformat(),
                                     report_only=report_only, seconds=policy.run_seconds)
        if run.get('active_slot'):
            await self.launch(run)
        return run

    async def launch(self, run):
        from aria.loop.git import TargetLock, OwnershipError
        root = Path(self.config.state_dir).expanduser().resolve()
        try:
            lock = TargetLock(root/'locks', 'weekly-launch-'+run['_id']).acquire()
        except OwnershipError:
            return
        try:
            return await self._launch_owned(run)
        finally:
            lock.release()

    async def _launch_owned(self, run):
        import psutil
        from datetime import timedelta
        from aria.api.deps import resolve_shell_service
        run = await self.db.improver_runs.find_one({'_id': run['_id']})
        if not run or not run.get('active_slot'):
            return
        if run.get('owner_pid'):
            try:
                process = psutil.Process(run['owner_pid'])
                if abs(process.create_time()-run.get('owner_started', 0)) < 0.01 and process.is_running():
                    if run['_id'] not in process.cmdline():
                        raise RuntimeError('Controller process identity is ambiguous; inspect the watched shell')
                    return
            except psutil.NoSuchProcess:
                pass
        if run.get('launched_at') and now()-run['launched_at'].replace(tzinfo=now().tzinfo) < timedelta(seconds=45):
            return
        from aria.loop.git import TargetLock, OwnershipError
        try:
            ownership = TargetLock(Path(self.config.state_dir).expanduser()/'locks', 'weekly-platform').acquire()
        except OwnershipError:
            return
        ownership.release()
        shells = self.shells or await resolve_shell_service(self.db)
        attempt = run.get('launch_attempt', 0)+1
        if attempt > 3:
            from aria.steward.weekly.execution import TrackedRuntime
            from aria.steward.weekly.reporting import render, publish, deliver
            policy, _ = load_policy(self.config)
            runtime = TrackedRuntime(self.db, run['_id'], policy.docker_binary,
                state_root=Path(self.config.state_dir).expanduser()/'runs'/run['_id'])
            for container in await self.db.improvement_containers.find({'run_id': run['_id'], 'active': True}).to_list(length=100):
                await runtime.stop(container['_id'])
            run.update(status='failed', error='Controller launch recovery limit reached')
            run['report'] = render(run)
            await self.store.finish(run, 'failed', error=run['error'], report=run['report'])
            await publish(self.db, run)
            await deliver(self.db, run)
            return
        name = settings.shells_tmux_session_prefix + 'weekly-' + run['_id'] + (f'-r{attempt}' if attempt>1 else '')
        # A tmux session can remain at a prompt after its controller dies. A fresh
        # recovery shell preserves that transcript and never types over human work.
        await self.db.improver_runs.update_one({'_id': run['_id']}, {'$set': {
            'shell_name': name, 'launch_attempt': attempt, 'launched_at': now()}})
        # The controller receives service configuration; candidate containers receive none of it.
        root = Path(self.config.state_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        configuration = root / 'controller-env.json'
        safe_fields = ('mongodb_uri', 'mongodb_database', 'obsidian_enabled', 'obsidian_vault_path',
                       'signal_breakglass_account', 'signal_breakglass_recipient', 'signal_cli_rpc_url', 'codex_binary')
        temporary = configuration.with_suffix('.new')
        fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump({key.upper(): getattr(settings, key) for key in safe_fields}, file)
        os.replace(temporary, configuration)
        api_root = Path(__file__).resolve().parents[3]
        command = shlex.join([sys.executable, '-m', 'aria.steward.weekly.runner', run['_id'],
                               '--policy', str(Path(self.config.policy_file).expanduser().resolve()),
                               '--state', str(root), '--environment', str(configuration)])
        try:
            shell = await shells.create_shell(name, workdir=str(api_root), launch_claude=False, launch_command=command)
            await shells.shells.update_one({'name': shell.name}, {'$set': {'tags': ['weekly-improvement', 'no-nudge'],
                'metadata.improvement_run_id': run['_id'], 'metadata.backend': 'codex'}})
            await self.db.improver_runs.update_one({'_id': run['_id']}, {'$set': {'shell_name': shell.name}})
        except Exception:
            if not await shells.tmux.has_session(name):
                raise

    async def status(self):
        error = None
        try:
            policy, fingerprint = load_policy(self.config)
            targets = list(policy.targets)
        except Exception as exc:
            error, targets, fingerprint = str(exc), [], None
        schedule = await self.db.schedules.find_one({'_id': 'weekly-platform-improvement'})
        active = await self.db.improver_runs.find_one({'active_slot': 'platform'})
        latest = await self.db.improver_runs.find_one({'schema_version': 2}, sort=[('created_at', -1)])
        return {'enabled': self.config.enabled, 'configured': error is None, 'configuration_error': error,
                'policy_hash': fingerprint, 'targets': targets, 'schedule': 'Thursday 04:00 America/New_York',
                'next_run_at': (schedule or {}).get('next_run_at'), 'active_run_id': (active or {}).get('_id'),
                'last_run_id': (latest or {}).get('_id'), 'run_seconds': 7200, 'spending_cap': None}

    async def maintenance(self):
        from aria.steward.weekly.deployment import Deployer
        from aria.steward.weekly.reporting import publish, deliver, render
        from aria.loop.git import TargetLock, OwnershipError
        policy, fingerprint = load_policy(self.config)
        root = Path(self.config.state_dir).expanduser()
        try:
            lock = TargetLock(root/'locks', 'weekly-maintenance').acquire()
        except OwnershipError:
            return
        try:
            changed_runs = set()
            active = await self.db.improver_runs.find_one({'active_slot': 'platform'})
            if active:
                await self.launch(active)
            pending = await self.db.improver_runs.find({'schema_version': 2, 'active_slot': {'$exists': False},
                '$or': [{'publication.status': {'$ne': 'published'}}, {'notification.status': {'$in': ['pending', 'unconfigured']}}]}).limit(10).to_list(length=10)
            for run in pending:
                if run.get('publication', {}).get('status') != 'published':
                    await publish(self.db, run)
                await deliver(self.db, run)
            watching = await self.db.improvement_findings.find({'watch.active': True}).limit(20).to_list(length=20)
            for finding in watching:
                target = policy.targets.get(finding['target'])
                if not target or finding.get('policy_hash') != fingerprint:
                    continue
                deployment = Deployer(root/'deployments'/finding['_id'], finding['target'], target)
                try:
                    health = await deployment.call('health', finding['deployment'])
                    watch = finding['watch']
                    if not health.get('healthy'):
                        rollback = await deployment.call('rollback', finding['deployment'])
                        await self.db.improvement_findings.update_one({'_id': finding['_id'], 'watch.active': True},
                            {'$set': {'disposition': 'rolled_back', 'watch.active': False, 'rollback': rollback}})
                        changed_runs.add(finding['run_id'])
                    elif now() >= watch['until'].replace(tzinfo=now().tzinfo):
                        await self.db.improvement_findings.update_one({'_id': finding['_id']}, {'$set': {
                            'watch.active': False, 'watch.clean': health.get('samples', 0) >= target.deployment.min_samples,
                            'watch.observed': health}})
                        changed_runs.add(finding['run_id'])
                except Exception as exc:
                    await self.db.improvement_findings.update_one({'_id': finding['_id']}, {'$set': {'watch.last_error': str(exc)[:1000]}})
            for run_id in changed_runs:
                run = await self.db.improver_runs.find_one({'_id': run_id})
                if not run or run.get('active_slot'):
                    continue
                run['findings'] = await self.db.improvement_findings.find({'run_id': run_id}).to_list(length=30)
                run['report'] = render(run)
                await self.db.improver_runs.update_one({'_id': run_id}, {'$set': {'findings': run['findings'], 'report': run['report'], 'publication.status': 'pending'}})
                await publish(self.db, run)
        finally:
            lock.release()
