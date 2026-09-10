from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "aria-boot-check"


def _load():
    loader = importlib.machinery.SourceFileLoader("aria_boot_check", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_boot_canary_waits_through_transport_and_readiness(monkeypatch):
    module = _load()
    outcomes = iter(
        [
            OSError("connection refused"),
            (503, {"phase": "database"}),
            (200, {"ready": True, "phase": "ready"}),
        ]
    )

    def fake_request(*args, **kwargs):
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(module, "_request", fake_request)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ok, payload, _elapsed = module._wait_ready("http://test", 30)
    assert ok is True
    assert payload["phase"] == "ready"


def test_mcp_contract_rejects_unreviewed_bridge(tmp_path):
    module = _load()
    source = tmp_path / "server.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    source.with_name("operations.py").write_text("# wrong dependency\n")
    assert module._mcp_contract(source)["ok"] is False


@pytest.mark.parametrize("failure", ["launchd", "mcp", "nodes", None])
def test_boot_exit_reflects_required_checks(monkeypatch, capsys, failure):
    module = _load()
    monkeypatch.setattr(module, "_wait_ready", lambda *a: (True, {"ready": True}, 0))
    monkeypatch.setattr(module, "_launchd_running", lambda *a: failure != "launchd")
    monkeypatch.setattr(module, "_mcp_contract", lambda *a: {"ok": failure != "mcp"})
    nodes = [{"node_id": name, "status": "online", "last_heartbeat_at": datetime.now(timezone.utc).isoformat()}
             for name in ["bens-macbook-pro", "mac-agents", "corsair-ai"]]
    monkeypatch.setattr(module, "_request", lambda *a: (503 if failure == "nodes" else 200, nodes))
    assert module.main([]) == (1 if failure else 0)
    assert json.loads(capsys.readouterr().out)["ok"] is (failure is None)


@pytest.mark.parametrize("age,status", [(300, "online"), (-300, "online"), (0, "offline")])
def test_node_health_rejects_stale_future_or_offline_heartbeats(age, status):
    module = _load()
    nodes = [{"node_id": "required", "status": status,
              "last_heartbeat_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()}]
    assert not module._node_health(200, nodes, ["required"], 120)["ok"]


def test_node_health_requires_named_nodes_but_excludes_sleeping_red():
    module = _load()
    nodes = [{"node_id": "red-linux", "status": "offline"}]
    assert not module._node_health(200, nodes, ["required"], 120)["ok"]
    nodes.append({"node_id": "required", "status": "online",
                  "last_heartbeat_at": datetime.now(timezone.utc).isoformat()})
    assert module._node_health(200, nodes, ["required"], 120)["ok"]
