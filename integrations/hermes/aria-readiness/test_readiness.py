"""Run with the deployed Hermes venv; no model or network calls."""
import importlib.util
from pathlib import Path
import sys
from unittest.mock import Mock
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("aria_readiness_test", Path(__file__).with_name("__init__.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "snapshot", lambda *_: {
        "pid": 1, "ready": True, "configured": True, "connected": True,
        "connection": "connected", "selected_count": len(module.REQUIRED), "missing_required": []})
    monkeypatch.setattr(module, "record", Mock())
    return module


def test_native_search_counts_as_model_access(plugin):
    plugin.before_request(request={"body": {"tools": [
        {"type": "function", "function": {"name": name}}
        for name in ["tool_search", "tool_describe", "tool_call"]]}})
    assert plugin.record.call_args.args[0]["model_visible"]["aria_accessible"] is True


def test_catalog_alone_does_not_prove_model_access(plugin):
    plugin.before_request(request={"body": {"tools": []}})
    assert plugin.record.call_args.args[0]["model_visible"]["aria_accessible"] is False


def test_unobserved_schema_is_unknown(plugin):
    plugin.before_request(request={"body": {}})
    assert plugin.record.call_args.args[0]["model_visible"] is None


def test_healthy_turn_does_not_reconnect_or_repeat_context(plugin, monkeypatch):
    recover = Mock()
    monkeypatch.setattr(plugin, "recover", recover)
    assert "tool_search" in plugin.before_turn(session_id="s")["context"]
    assert plugin.before_turn(session_id="s") is None
    recover.assert_not_called()


def test_disabled_aria_is_not_reenabled(plugin, monkeypatch):
    recover = Mock()
    monkeypatch.setattr(plugin, "recover", recover)
    monkeypatch.setattr(plugin, "snapshot", lambda *_: {
        "ready": False, "configured": True, "connected": False,
        "connection": "disabled", "missing_required": sorted(plugin.REQUIRED)})
    plugin.before_turn(session_id="s")
    recover.assert_not_called()


def test_recovery_refreshes_receipt_and_only_discovers_aria(plugin, monkeypatch):
    discover = Mock()
    reconnect = Mock(return_value=False)
    for name, values in {
        "tools": {},
        "tools.mcp_tool_discovery": {"discover_mcp_tools": discover},
        "tools.mcp_tool_loop": {"reconnect_mcp_server": reconnect},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(plugin.threading, "Thread",
                        lambda *, target, **_: SimpleNamespace(start=target))
    plugin.recover()
    reconnect.assert_called_once_with("aria")
    discover.assert_called_once_with(allowed_mcp_names=["aria"])
    assert plugin.record.call_args.args[0]["ready"] is True
    assert plugin._recovery_running is False
