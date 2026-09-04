from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


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


def test_mcp_contract_fingerprint_is_stable(tmp_path):
    module = _load()
    source = tmp_path / "server.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    first = module._mcp_contract(source)
    second = module._mcp_contract(source)
    assert first["ok"] is True
    assert first["sha256"] == second["sha256"]
