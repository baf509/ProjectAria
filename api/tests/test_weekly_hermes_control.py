import importlib.machinery
import importlib.util
import json
from types import SimpleNamespace

import pytest

from .test_weekly_deploy_failures import ADAPTER


@pytest.fixture
def controller(tmp_path, monkeypatch):
    path = ADAPTER.parent/'aria-weekly-hermes-control'
    loader = importlib.machinery.SourceFileLoader('hermes_control_test', str(path))
    spec = importlib.util.spec_from_loader(loader.name,loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    monkeypatch.setattr(m,'HOME',tmp_path)
    monkeypatch.setattr(m,'STATE',tmp_path/'owner.json')
    monkeypatch.setattr(m,'MARKER',tmp_path/'marker.json')
    monkeypatch.setattr(m,'owner',lambda:{'pid':100,'started':1.0})
    fake = SimpleNamespace(pid=200,create_time=lambda:2.0,send_signal=lambda _:None)
    monkeypatch.setattr(m,'process',lambda:(fake,{
        'gateway_state':'draining' if m.MARKER.exists() else 'running','active_agents':0}))
    def wait(predicate, seconds=120):
        if not predicate():
            raise TimeoutError('No acknowledgement')
    monkeypatch.setattr(m,'wait_for',wait)
    return m


def test_foreign_drain_is_never_replaced_or_released(controller):
    m = controller
    original = '{"principal":"another-controller"}'
    m.MARKER.write_text(original)
    with pytest.raises(ValueError,match='Existing'):
        m.drain()
    m.release()
    assert m.MARKER.read_text() == original


def test_drain_requires_observed_acknowledgement_and_cleans_timeout(controller,monkeypatch):
    m = controller
    p, _ = m.process()
    monkeypatch.setattr(m,'process',lambda:(p,{'gateway_state':'running','active_agents':0}))
    with pytest.raises(TimeoutError):
        m.drain()
    assert not m.MARKER.exists() and not m.STATE.exists()


def test_busy_hermes_is_not_reloaded(controller,monkeypatch):
    m = controller
    m.drain()
    p, _ = m.process()
    monkeypatch.setattr(m,'process',lambda:(p,{'gateway_state':'draining','active_agents':1}))
    with pytest.raises(ValueError,match='acknowledged idle drain'):
        m.reload()


def test_changed_ownership_blocks_release(controller):
    m = controller
    m.drain()
    m.MARKER.write_text('{"principal":"later-controller"}')
    with pytest.raises(ValueError,match='Another operation'):
        m.release()
    assert json.loads(m.MARKER.read_text())['principal'] == 'later-controller'


def test_owned_drain_releases_only_its_marker(controller):
    m = controller
    m.drain()
    assert m.MARKER.exists() and m.STATE.exists()
    m.release()
    assert not m.MARKER.exists() and not m.STATE.exists()


def test_changed_gateway_process_cannot_acknowledge_drain(controller,monkeypatch):
    m = controller
    original = m.process
    def process():
        p, state = original()
        if m.MARKER.exists():
            p = SimpleNamespace(pid=300,create_time=lambda:3.0)
        return p,state
    monkeypatch.setattr(m,'process',process)
    with pytest.raises(TimeoutError):
        m.drain()
    assert not m.MARKER.exists()
