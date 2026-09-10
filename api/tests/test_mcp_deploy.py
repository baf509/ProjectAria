"""Independent MCP deployment verifies bytes and can restore legacy entry points."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def deploy(tmp_path, monkeypatch):
    path = Path(__file__).parents[2] / 'scripts/aria-deploy-mcp'
    loader = importlib.machinery.SourceFileLoader('mcp_deploy', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, 'RELEASES', tmp_path)
    return module


def test_release_rejects_tampering_and_missing_files(deploy, tmp_path):
    release = tmp_path / 'bundle'
    release.mkdir()
    for f in deploy.FILES:
        p = release / f
        p.parent.mkdir(exist_ok=True, parents=True)
        p.write_text('reviewed')
    files = {f: deploy.digest(release / f) for f in deploy.FILES}
    identity = deploy.hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    (release / 'release.json').write_text(json.dumps({'files': files, 'content_sha256': identity}))
    assert deploy.verify(release)['content_sha256'] == identity
    (release / 'mcp/server.py').write_text('unreviewed')
    with pytest.raises(ValueError, match='hash mismatch'):
        deploy.verify(release)


def test_restore_legacy_files_and_previous_pointer(deploy, tmp_path, monkeypatch):
    app = tmp_path / 'app'; app.mkdir()
    monkeypatch.setattr(deploy, 'APP', app)
    (app / 'current').symlink_to(tmp_path / 'new')
    target = app / 'server.py'; target.symlink_to('current/mcp/server.py')
    backup = tmp_path / 'saved'; backup.write_bytes(b'old implementation')
    deploy.restore({'entries': [{'path': str(target), 'backup': str(backup)}], 'previous': None})
    assert target.read_bytes() == b'old implementation' and not target.is_symlink()
    assert not (app / 'current').exists()


def test_install_and_rollback_include_hermes_readiness_plugin(deploy, tmp_path, monkeypatch):
    app = tmp_path / 'app'; app.mkdir()
    plugin = tmp_path / 'plugin'; plugin.mkdir()
    launcher = tmp_path / 'launcher'; boot = tmp_path / 'boot'
    monkeypatch.setattr(deploy, 'APP', app)
    monkeypatch.setattr(deploy, 'HERMES_PLUGIN', plugin)
    monkeypatch.setattr(deploy, 'LAUNCHER', launcher)
    monkeypatch.setattr(deploy, 'BOOT', boot)
    monkeypatch.setattr(deploy, 'verify', lambda path: {})
    # Redirect the installer's existing service-backup root for this offline test.
    real_path = deploy.Path
    monkeypatch.setattr(deploy, 'Path', lambda value: tmp_path if value == '/Users/ben/Services/backups' else real_path(value))
    paths = [app / 'server.py', app / 'operations.py', launcher, boot, plugin / '__init__.py']
    for p in paths: p.write_text('previous')
    release = tmp_path / 'release'; release.mkdir()
    (release / 'release.json').write_text('{}')
    source = release / 'integrations/hermes/aria-readiness/__init__.py'
    source.parent.mkdir(parents=True); source.write_text('new readiness')
    deploy.install(release)
    assert (plugin / '__init__.py').resolve() == source
    receipt = json.loads(next(tmp_path.glob('aria-mcp-*/installation.json')).read_text())
    assert len(receipt['entries']) == 5
    deploy.restore(receipt)
    assert all(p.read_text() == 'previous' and not p.is_symlink() for p in paths)
