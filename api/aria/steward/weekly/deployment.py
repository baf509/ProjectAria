"""Fixed, hash-pinned controller adapter protocol; worker output is never executed here."""
from __future__ import annotations

import json
import os
from pathlib import Path

from aria.loop.runtime import process
from aria.steward.weekly.policy import digest


class Deployer:
    def __init__(self, directory, target_id, target):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.target_id, self.target = target_id, target

    async def call(self, operation, request):
        if operation not in {'preflight', 'apply', 'health', 'rollback', 'reconcile'}:
            raise ValueError('Unknown deployment operation')
        if not self.target.deployment:
            raise ValueError('No qualified deployment adapter for target')
        command = self.target.deployment.command
        executable = command.validate_executable()
        value = {**request, 'target_id': self.target_id, 'operation': operation}
        path = self.directory / (operation + '-' + digest(value)[:16] + '.json')
        if not path.exists():
            path.write_text(json.dumps(value, default=str))
            path.chmod(0o600)
        env = {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'TMPDIR', 'LANG'}}
        argv = [str(executable), operation, str(path)]
        if command.configuration:
            argv.append(str(Path(command.configuration).expanduser().resolve()))
        code, output = await process(argv,
                                      env=env, timeout=command.timeout_seconds)
        if code:
            raise RuntimeError('Deployment adapter failed: ' + output.decode(errors='replace')[-1500:])
        result = json.loads(output.decode().splitlines()[-1])
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise RuntimeError('Adapter did not attest success')
        return result
