import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest


def load():
    loader = importlib.machinery.SourceFileLoader('aria_deploy_mac', str(Path(__file__).parents[2] / 'scripts/aria-deploy-mac'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_verify_refuses_changed_artifact_and_paths_outside_release(tmp_path, monkeypatch):
    module = load()
    monkeypatch.setattr(module, 'RELEASES', tmp_path)
    release = tmp_path / 'release'
    release.mkdir()
    file = release / 'artifact'
    file.write_text('reviewed')
    manifest = {'build': 'test', 'files': {}, 'artifacts': {'artifact': module.digest(file)}}
    (release / 'release.json').write_text(json.dumps(manifest))
    assert module.verify(release)['build'] == 'test'
    file.write_text('changed after review')
    with pytest.raises(ValueError, match='hash mismatch'):
        module.verify(release)
    external = tmp_path / 'outside'
    external.write_text('not in release')
    manifest['artifacts'] = {'../outside': module.digest(external)}
    (release / 'release.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='hash mismatch'):
        module.verify(release)


def test_busy_inference_prevents_any_activation_changes(tmp_path, monkeypatch):
    module = load()
    monkeypatch.setattr(module, 'verify', lambda *a: {'build': 'test'})
    monkeypatch.setattr(module, 'run', lambda *a, **kw: None)
    monkeypatch.setattr(module, 'api_key', lambda: 'test')
    def busy(*a):
        raise ValueError('busy')
    monkeypatch.setattr(module, 'require_idle', busy)
    monkeypatch.setattr(module, 'install_launchers', lambda *a: pytest.fail('must not change launchers'))
    monkeypatch.setattr(module, 'point_to', lambda *a: pytest.fail('must not change current'))
    with pytest.raises(ValueError, match='busy'):
        module.activate(tmp_path)


def test_rollback_refuses_a_newer_active_release(tmp_path, monkeypatch):
    module = load()
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'current').symlink_to(tmp_path / 'newer')
    monkeypatch.setattr(module, 'APP', app)
    backup = tmp_path / 'backup'
    backup.mkdir()
    (backup / 'activation.json').write_text(json.dumps({'release': str(tmp_path / 'older')}))
    monkeypatch.setattr(module, 'api_key', lambda: pytest.fail('must refuse before runtime access'))
    with pytest.raises(ValueError, match='Active release changed'):
        module.rollback(backup)


def test_failed_initial_activation_restores_launchers_and_legacy_tree(tmp_path, monkeypatch):
    module = load()
    app, binary, backups, release = [tmp_path / name for name in ('app', 'bin', 'backups', 'release')]
    for directory in (app, binary, backups, release):
        directory.mkdir()
    (release / 'release.json').write_text('{}')
    names = ['run-aria-api', 'run-aria-ui', 'run-aria-ui-server', 'run-aria-node']
    for name in names:
        (binary / name).write_text('original ' + name)
    monkeypatch.setattr(module, 'APP', app)
    monkeypatch.setattr(module, 'BIN', binary)
    monkeypatch.setattr(module, 'Path', lambda p: backups if str(p) == '/Users/ben/Services/backups' else Path(p))
    monkeypatch.setattr(module, 'verify', lambda *a: {'build': 'new'})
    monkeypatch.setattr(module, 'run', lambda *a, **kw: None)
    monkeypatch.setattr(module, 'api_key', lambda: 'test')
    monkeypatch.setattr(module, 'require_idle', lambda *a: None)
    monkeypatch.setattr(module, 'get', lambda *a, **kw: {'sha': 'old'})

    def install(_release, backup):
        for name in names:
            (backup / name).write_bytes((binary / name).read_bytes())
            (binary / name).write_text('new launcher')

    restarts = []
    def restart(label):
        restarts.append(label)
        if len(restarts) == 1:
            assert (app / 'current').resolve() == release
            raise RuntimeError('simulated launch failure')

    monkeypatch.setattr(module, 'install_launchers', install)
    monkeypatch.setattr(module, 'restart', restart)
    with pytest.raises(RuntimeError, match='simulated launch failure'):
        module.activate(release)
    assert not (app / 'current').is_symlink()
    assert [(binary / name).read_text() for name in names] == ['original ' + name for name in names]
    assert restarts[1:] == module.JOBS
    receipt = json.loads(next(backups.glob('*/activation.json')).read_text())
    assert receipt['status'] == 'rolled_back'
    assert receipt['previous'] is None
