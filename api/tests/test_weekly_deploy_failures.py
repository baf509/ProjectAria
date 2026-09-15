"""Exercise real deploy subprocesses under failed health and concurrent edits."""
import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ADAPTER = Path(__file__).resolve().parents[2]/'scripts/aria-weekly-deploy-adapter'


@pytest.fixture
def deployment(tmp_path):
    repo = tmp_path/'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git','-C',str(repo),*args], stderr=subprocess.DEVNULL, text=True).strip()
    git('init','-b','main')
    source = repo/'plugin.py'
    source.write_text('VALUE = "before"\n')
    git('add','.')
    git('-c','user.name=Test','-c','user.email=test@local','commit','-m','baseline')
    base = git('rev-parse','HEAD')
    source.write_text('VALUE = "after"\n')
    git('add','.')
    git('-c','user.name=Test','-c','user.email=test@local','commit','-m','candidate')
    candidate = git('rev-parse','HEAD')
    live = tmp_path/'live.py'
    live.write_text('VALUE = "before"\n')
    live.chmod(0o640)
    mode = tmp_path/'probe-mode'
    mode.write_text('healthy')
    health = tmp_path/'health'
    health.write_text(f'''#!{sys.executable}
import json
from pathlib import Path
live=Path({str(live)!r})
mode=Path({str(mode)!r}).read_text()
changed='after' in live.read_text()
if mode=='later-edit' and changed:
    live.write_text('VALUE = "human-edit"\\n')
print(json.dumps({{'healthy': not(changed and mode!='healthy'), 'samples':12}}))
''')
    health.chmod(0o700)
    registry = tmp_path/'registry.json'
    registry.write_text(json.dumps({'state_dir':str(tmp_path/'receipts'),'targets':{'hermes':{
        'kind':'files','repository':str(repo),'destinations':[str(live)],
        'files':{'plugin.py':str(live)},
        'health_command':{'executable':str(health),'sha256':hashlib.sha256(health.read_bytes()).hexdigest()}}}}))
    def call(operation, **extra):
        request = {'target_id':'hermes','finding_id':'fixture','repository':str(repo),
                   'base_revision':base,'candidate':candidate,**extra}
        path = tmp_path/'request.json'
        path.write_text(json.dumps(request))
        result = subprocess.run([str(ADAPTER),operation,str(path),str(registry)],capture_output=True,text=True,timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout.splitlines()[-1])
    return call, live, mode, tmp_path


def test_failed_consumer_health_restores_exact_bytes_and_mode(deployment):
    call, live, mode, root = deployment
    previous = live.read_bytes()
    pre = call('preflight')
    mode.write_text('unhealthy')
    with pytest.raises(RuntimeError, match='Post-deployment health failed'):
        call('apply',expected_identity=pre['identity'])
    assert live.read_bytes() == previous and live.stat().st_mode & 0o777 == 0o640
    assert json.loads((root/'receipts/fixture.json').read_text())['status'] == 'rolled_back'


def test_failed_deployment_does_not_overwrite_a_later_live_edit(deployment):
    call, live, mode, root = deployment
    pre = call('preflight')
    mode.write_text('later-edit')
    with pytest.raises(RuntimeError, match='later live edit'):
        call('apply',expected_identity=pre['identity'])
    assert 'human-edit' in live.read_text()
    receipt = json.loads((root/'receipts/fixture.json').read_text())
    assert receipt['status'] == 'recovery_blocked_later_edit_or_backup_changed'
    with pytest.raises(RuntimeError, match='later live edit'):
        call('reconcile')
    assert 'human-edit' in live.read_text()


def test_full_file_mapping_refuses_live_source_drift_even_after_preflight(deployment):
    call, live, _, _ = deployment
    live.write_text('VALUE = "manual-before-review"\n')
    pre = call('preflight')
    with pytest.raises(RuntimeError, match='reviewed baseline'):
        call('apply',expected_identity=pre['identity'])
    assert 'manual-before-review' in live.read_text()


def test_modified_backup_is_not_restored(deployment):
    call, live, _, root = deployment
    pre = call('preflight')
    result = call('apply',expected_identity=pre['identity'])
    receipt = json.loads(Path(result['receipt']).read_text())
    Path(next(iter(receipt['backups'].values()))).write_text('corrupt')
    with pytest.raises(RuntimeError, match='backup changed'):
        call('rollback')
    assert 'after' in live.read_text()


def test_replaced_destination_symlink_is_refused(deployment):
    call, live, _, root = deployment
    other = root/'human.py'
    other.write_bytes(live.read_bytes())
    live.unlink()
    live.symlink_to(other)
    with pytest.raises(RuntimeError, match='regular file'):
        call('preflight')
    assert other.read_text() == 'VALUE = "before"\n'


@pytest.mark.parametrize('failure', [False, True])
def test_admission_is_held_through_install_recovery_and_health(deployment,failure):
    call, live, mode, root = deployment
    gate = root/'admission'
    hooks = {}
    for action in ('hold','release'):
        path = root/action
        path.write_text(f'#!{sys.executable}\nfrom pathlib import Path\np=Path({str(gate)!r})\n'
                        + ("p.open('x').close()\n" if action=='hold' else 'p.unlink()\n'))
        path.chmod(0o700)
        hooks[action] = {'executable':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    registry_path = root/'registry.json'
    registry = json.loads(registry_path.read_text())
    target = registry['targets']['hermes']
    target['quiesce_command'], target['resume_command'] = hooks['hold'], hooks['release']
    health = root/'health'
    script = health.read_text().replace("changed='after' in live.read_text()",
        f"changed='after' in live.read_text()\nif changed: assert Path({str(gate)!r}).exists()")
    health.write_text(script)
    target['health_command']['sha256'] = hashlib.sha256(health.read_bytes()).hexdigest()
    registry_path.write_text(json.dumps(registry))
    pre = call('preflight')
    assert not gate.exists()
    if failure:
        mode.write_text('unhealthy')
        with pytest.raises(RuntimeError,match='Post-deployment health'):
            call('apply',expected_identity=pre['identity'])
        assert 'before' in live.read_text()
    else:
        call('apply',expected_identity=pre['identity'])
        assert 'after' in live.read_text()
        call('rollback')
        assert 'before' in live.read_text()
    assert not gate.exists()


@pytest.mark.parametrize('value', [True, float('nan'), float('inf')])
def test_config_rejects_boolean_and_nonfinite_numeric_values(value):
    loader = importlib.machinery.SourceFileLoader('adapter_numeric_test',str(ADAPTER))
    spec = importlib.util.spec_from_loader(loader.name,loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    with pytest.raises(ValueError):
        module.field_updates({'ratio':1},{'ratio':value},{'ratio':{'type':'float','min':0,'max':1}})
