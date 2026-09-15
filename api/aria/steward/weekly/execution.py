"""Immutable candidates, independently executed checks and compare-and-swap Git promotion."""
from __future__ import annotations

import json
from pathlib import Path

from aria.guard.policy import is_protected
from aria.loop.git import GitWorkspace, TargetLock, git
from aria.loop.runtime import ContainerRuntime

PROTECTED = ('api/aria/steward/weekly/', 'api/aria/loop/', 'api/aria/deployment/',
             'api/aria/steward/improve.py', 'scripts/aria-deploy', 'scripts/install-aria',
             'scripts/aria-service-restart', 'api/aria/scheduler/', 'api/aria/api/routes/improve.py',
             'api/aria/main.py', 'AGENTS.md', '.github/', '.git')


def matches(path, scope):
    return any(path == p.rstrip('/') or p.endswith('/') and path.startswith(p) for p in scope)


def validate_changes(paths, target):
    if not paths or len(paths) > 20:
        raise ValueError('Candidate is empty or exceeds 20 changed files')
    for path in paths:
        if (is_protected(path, target.repository) or matches(path, target.protected_paths)
                or any(path.startswith(p) for p in PROTECTED) or not matches(path, target.allowed_paths)):
            raise ValueError('Candidate outside mutable scope: ' + path)


class TrackedRuntime(ContainerRuntime):
    def __init__(self, db, run_id, binary, state_root):
        super().__init__(binary, state_root=state_root)
        self.db, self.run_id = db, run_id

    async def start(self, name, *args, **kwargs):
        await self.db.improvement_containers.update_one({'_id': name},
            {'$set': {'run_id': self.run_id, 'active': True}}, upsert=True)
        return await super().start(name, *args, **kwargs)

    async def stop(self, name):
        await super().stop(name)
        await self.db.improvement_containers.update_one({'_id': name}, {'$set': {'active': False}})


class Workspace:
    def __init__(self, root, target_id, policy, run_id, stage, runtime=None):
        self.root = Path(root)
        self.target_id, self.policy, self.run_id, self.stage = target_id, policy, run_id, stage
        self.git = GitWorkspace(self.root / 'git')
        self.runtime = runtime or ContainerRuntime(state_root=self.root)
        self.container = f'aria-weekly-{run_id}-{target_id}-{stage}'
        self.path = self.root / 'workspace'
        self.started = False

    async def prepare(self, *, readonly=False, seconds=900, evidence=None):
        self.base = (await git('-C', self.policy.repository, 'rev-parse', 'refs/heads/'+self.policy.branch)).decode().strip()
        await self.git.initialize(self.policy.repository, self.base)
        await self.git.checkout(self.base, self.path)
        if evidence is not None:
            (self.path / "__aria_review_evidence.json").write_text(json.dumps(evidence, default=str))
        await self.runtime.preflight(self.policy.image)
        self.started = True
        await self.runtime.start(self.container, self.path, self.policy, readonly=readonly, seconds=seconds)
        return self.base

    async def command(self, command):
        return await self.runtime.execute(self.container, ['/bin/sh', '-c', command], 120)

    async def stop(self):
        if self.started:
            await self.runtime.stop(self.container)
            self.started = False

    async def checks(self, revision, check_ids, label):
        results = {}
        path = self.root / ('check-' + label)
        await self.git.checkout(revision, path)
        name = self.container + '-' + label
        try:
            await self.runtime.start(name, path, self.policy, readonly=True,
                                     assets=Path(self.policy.assets), seconds=sum(self.policy.checks[x].timeout_seconds for x in check_ids)+30)
            for key in check_ids:
                check = self.policy.checks[key]
                result = await self.runtime.execute(name, check.argv, check.timeout_seconds)
                results[key] = result
        finally:
            await self.runtime.stop(name)
        return results

    async def verify(self, check_id):
        await self.stop()  # Includes descendants and exports files before snapshotting.
        candidate = await self.git.snapshot(self.path, self.base, self.stage)
        paths = await self.git.changes(self.base, candidate)
        validate_changes(paths, self.policy)
        before = await self.checks(self.base, [check_id], 'before')
        after = await self.checks(candidate, list(dict.fromkeys([check_id]+self.policy.regression_check_ids)), 'after')
        if before[check_id]['exit_code'] == 0:
            raise ValueError('Trusted acceptance check does not reproduce a baseline failure')
        if any(v['exit_code'] != 0 for v in after.values()):
            raise ValueError('Candidate failed trusted acceptance/regression checks')
        return {'candidate': candidate, 'base': self.base, 'paths': paths, 'before': before, 'after': after,
                'git_dir': str(self.git.git_dir)}


async def merge_candidate(target, evidence, lock_root, identifier):
    lock = TargetLock(Path(lock_root), target.repository).acquire()
    try:
        base, candidate = evidence['base'], evidence['candidate']
        current = (await git('-C', target.repository, 'rev-parse', 'refs/heads/'+target.branch)).decode().strip()
        if current != base:
            raise ValueError('Destination advanced; candidate must be reverified')
        # Canonical checkout may contain unrelated human work. Never check out,
        # reset or mutate it. A checked-out destination is not safe for update-ref.
        worktrees = (await git('-C', target.repository, 'worktree', 'list', '--porcelain')).decode()
        if 'branch refs/heads/'+target.branch+'\n' in worktrees:
            if (await git('-C', target.repository, 'status', '--porcelain')).strip():
                raise ValueError('Destination checkout is dirty; merge deferred')
            # Guarded ff-only merge updates index/worktree consistently.
            checked_branch = (await git('-C', target.repository, 'symbolic-ref', '--short', 'HEAD')).decode().strip()
            if checked_branch != target.branch:
                raise ValueError('Destination branch is checked out in another worktree')
        await git('-C', target.repository, 'fetch', '--no-tags', evidence['git_dir'], candidate)
        await git('-C', target.repository, 'update-ref', 'refs/heads/improve/'+identifier, candidate)
        if 'branch refs/heads/'+target.branch+'\n' in worktrees:
            await git('-C', target.repository, 'merge', '--ff-only', candidate)
        else:
            await git('-C', target.repository, 'update-ref', 'refs/heads/'+target.branch, candidate, base)
        return candidate
    finally:
        lock.release()
