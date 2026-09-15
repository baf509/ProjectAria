#!/usr/bin/env python3
"""Activate the pinned session release, then verify the installed Hermes contract."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import time

import httpx
import yaml
from dotenv import load_dotenv

BASE = Path('/Users/ben/Services/backups/aria-session-deploy-20260915')
API = Path('/Users/ben/Services/releases/ProjectAria/20260915T122214Z-b070d82-cd796da4b148')
MCP = Path('/Users/ben/Services/releases/aria-mcp/20260915T122207Z-f1885fc5d81e')
OLD_API = Path('/Users/ben/Services/releases/ProjectAria/20260915T091340Z-029986d-1a274be96a53')
OLD_MCP = Path('/Users/ben/Services/releases/aria-mcp/20260914T101143Z-08d03b3fbc0a')
API_LINK = Path('/Users/ben/Services/apps/ProjectAria/current')
MCP_LINK = Path('/Users/ben/Services/apps/aria-mcp/current')
HP = '/Users/ben/Services/apps/hermes-agent/venv/bin/python'
AP = '/Users/ben/Services/apps/ProjectAria/api/.venv/bin/python'
HERMES = Path('/Users/ben/Services/data/hermes-home')
TASK = '6aa93854ea2ccb4b7f2c8d15'
OLD_HINT_SHA = '691c37298656445e8efa5f718c13a829b97720d2fa452995663bb4904b60f1f1'


def sha(value):
    return hashlib.sha256(value).hexdigest()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def command(args, log=None, timeout=600):
    if log:
        with (BASE / log).open('w') as output:
            result = subprocess.run([str(a) for a in args], stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
        if result.returncode:
            raise RuntimeError('Command failed; inspect ' + str(BASE / log))
    else:
        subprocess.run([str(a) for a in args], check=True, timeout=timeout)


def gateway():
    from gateway.control_socket import query_gateway_control
    value = query_gateway_control(HERMES, 'status')
    if not value or value.get('kind') != 'hermes-gateway':
        raise RuntimeError('Hermes gateway did not answer its native control socket')
    return value


def verify_sources():
    for link, previous, target in [(API_LINK, OLD_API, API), (MCP_LINK, OLD_MCP, MCP)]:
        if link.resolve() not in {previous, target}:
            raise RuntimeError('Another release has been installed since staging: ' + str(link))
    command([AP, API / 'scripts/aria-deploy-mac', 'verify', API])
    command([HP, API / 'scripts/aria-deploy-mcp', 'verify', MCP], 'mcp-verify.json')
    old = yaml.safe_load((HERMES / 'config.yaml').read_text())['agent']['environment_hint']
    new = (MCP / 'integrations/hermes/environment-hint.md').read_text()
    if sha(old.encode()) != OLD_HINT_SHA and old != new:
        raise RuntimeError('Hermes guidance changed since staging; review before replacing it')
    if len(new) > 3000:
        raise RuntimeError('Hermes guidance exceeds the reviewed size limit')
    return new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    os.environ['HERMES_HOME'] = str(HERMES)
    os.environ['HERMES_GATEWAY_SESSION'] = '1'
    os.environ['HERMES_INTERACTIVE'] = '1'
    load_dotenv(HERMES / '.env', override=True)
    sys.path.insert(0, '/Users/ben/Services/apps/hermes-agent')
    new_hint = verify_sources()
    state = gateway()
    if state.get('active_agents', 0) or state.get('restart_requested'):
        raise RuntimeError('Hermes has active work or a pending restart; retry after it drains')
    controller = runpy.run_path(str(API / 'scripts/aria-deploy-mac'))
    controller['require_idle'](controller['api_key']())
    if args.check_only:
        print('Pinned releases, current deployment, Hermes guidance and idle checks passed.')
        return
    command(['/usr/bin/sudo', '-n', 'true'])
    receipt = {'status': 'activating', 'api_release': str(API), 'mcp_release': str(MCP),
               'source_head': json.loads((API / 'release.json').read_text())['source_head'],
               'started_at': datetime.now(timezone.utc).isoformat()}
    save(BASE / 'activation.json', receipt)
    try:
        if API_LINK.resolve() != API:
            print('Activating API/UI and ARIA node...', flush=True)
            command([AP, API / 'scripts/aria-deploy-mac', 'activate', API], 'api-activation.json')
        if MCP_LINK.resolve() != MCP:
            print('Installing the matching Hermes MCP release...', flush=True)
            command([HP, API / 'scripts/aria-deploy-mcp', 'install', MCP], 'mcp-installation.json')
        path = HERMES / 'config.yaml'
        before = path.read_bytes()
        config = yaml.safe_load(before)
        old_hint = config['agent']['environment_hint']
        if old_hint != new_hint:
            if sha(old_hint.encode()) != OLD_HINT_SHA:
                raise RuntimeError('Hermes guidance changed during activation')
            backup = BASE / ('hermes-config-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.yaml')
            shutil.copy2(path, backup)
            backup.chmod(0o600)
            config['agent']['environment_hint'] = new_hint
            temporary = path.with_name('config.yaml.session-deploy-tmp')
            temporary.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
            temporary.chmod(path.stat().st_mode & 0o777)
            if path.read_bytes() != before:
                temporary.unlink()
                raise RuntimeError('Hermes configuration changed during preparation')
            temporary.replace(path)
        state = gateway()
        if state.get('active_agents', 0):
            raise RuntimeError('Hermes has new active work; rerun when it drains to finish reload')
        old_pid = state['pid']
        print('Requesting a native, graceful Hermes gateway restart...', flush=True)
        os.kill(old_pid, signal.SIGUSR1)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Hermes has not drained; no force kill was attempted')
        # launchd owns relaunch. Kickstart only after the old process exits.
        command(['/usr/bin/sudo', '-n', '/bin/launchctl', 'kickstart', 'system/com.ben.devbox.hermes-gateway'])
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            try:
                live = gateway()
                ready = json.loads((HERMES / 'aria-tools-ready.json').read_text())
                if (live['pid'] != old_pid and ready.get('pid') == live['pid'] and ready.get('ready')
                        and live.get('platforms', {}).get('signal', {}).get('state') == 'connected'):
                    break
            except (OSError, ValueError, RuntimeError):
                pass
            time.sleep(2)
        else:
            raise RuntimeError('Hermes reload/readiness deadline exceeded; inspect gateway status')
        save(BASE / 'hermes-after.json', {'pid': live['pid'], 'previous_pid': old_pid,
             'signal_connected': True, 'readiness': ready})
        print('Checking live tool discovery, routing, browser, PDF and 3090 vision...', flush=True)
        manifest = json.loads((MCP / 'release.json').read_text())
        exposure = BASE / ('mcp-live-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json')
        command([HP, MCP / 'integrations/hermes/verify-aria-exposure.py',
                 '--expected-sha256', manifest['files']['mcp/server.py'],
                 '--expected-operations-sha256', manifest['files']['mcp/operations.py'],
                 '--model', 'Red-Qwen3.8-27B-PARO-INT5', '--task-id', TASK, '--out', exposure], 'mcp-live-check.log')
        command([HP, API / 'scripts/aria-tools-check', '--vision', '--output', BASE / 'functional-after.json'], 'functional-after.log')
        checks = json.loads((BASE / 'functional-after.json').read_text())
        if not checks['ok'] or any(c['status'] != 'passed' for c in checks['checks'].values()):
            raise RuntimeError('Not all functional checks passed')
        command([AP, MCP / 'scripts/aria-boot-check', '--mcp-source', MCP / 'mcp/server.py', '--wait', '60'], 'boot-after.json')
        receipt.update(status='verified', finished_at=datetime.now(timezone.utc).isoformat(),
                       checks=len(checks['checks']), mcp_exposure=str(exposure), hermes_pid=live['pid'])
        save(BASE / 'activation.json', receipt)
        _, key = runpy.run_path(str(API / 'scripts/aria-route-task'))['resolve_target'](None)
        with httpx.Client(base_url='http://127.0.0.1:8200', headers={'X-API-Key': key}, timeout=30) as client:
            notes = ('Session deployment activated and verified: API/UI b070d82, matching independent MCP, '
                     'Hermes gateway reloaded with Signal connected, all nine functional checks including 3090 vision passed. '
                     'Model runtimes unchanged. Evidence: ' + str(BASE))
            response = client.patch('/api/v1/todos/' + TASK, json={'notes': notes})
            response.raise_for_status()
            content = ('# ARIA session deployment verified\n\n' + notes + '\n\n'
                       + 'API/UI release: `' + str(API) + '`\n\n'
                       + 'MCP release: `' + str(MCP) + '`\n\n'
                       + 'The tested session merge b070d82 is live. Newer weekly-platform work remains outside this release. '
                       + 'Existing interactive Hermes processes refresh cached tool definitions with `/reload-mcp` or on their next start.\n')
            response = client.post('/api/v1/obsidian/publish', json={'title': 'ARIA Session Deployment',
                                   'project': 'infrastructure', 'doc_type': 'Analysis', 'content': content})
            response.raise_for_status()
            save(BASE / 'publication-after.json', response.json())
            response = client.post('/api/v1/todos/' + TASK + '/done', json={})
            response.raise_for_status()
        print('Session deployment complete: API/UI, MCP and Hermes verified; all nine functional checks passed.')
    except Exception as exc:
        receipt.update(status='needs-attention', error_type=type(exc).__name__, error=str(exc))
        save(BASE / 'activation.json', receipt)
        raise


if __name__ == '__main__':
    main()
