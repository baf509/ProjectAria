from __future__ import annotations

import json
import importlib.util
from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_real_signal_failure_modes_have_routing_regressions():
    cases = json.loads(
        (ROOT / "integrations" / "hermes" / "tool-selection-evals.json").read_text()
    )
    ids = {case["id"] for case in cases}
    assert {
        "inspect-corsair-shell",
        "remote-shell-not-local-tmux",
        "close-shell",
        "create-claude-coding-work",
        "create-codex-interactive-shell",
        "create-pi-coding-work",
        "backend-unavailable",
        "approval-control-message",
        "steer-active-run",
    } <= ids
    assert all("prompt" in case and "first_tool" in case for case in cases)


def test_environment_hint_is_short_authoritative_and_has_no_historical_catalog():
    hint = (ROOT / "integrations" / "hermes" / "environment-hint.md").read_text()
    assert "fleet_status" in hint
    assert "create_shell" in hint
    assert "create_coding_session" in hint
    assert "Tailscale" in hint
    assert "Pool" not in hint
    assert ":810" not in hint
    assert len(hint) < 3000


def test_tool_selection_scorer_checks_tool_arguments_and_forbidden_fallbacks():
    script = ROOT / "integrations" / "hermes" / "evaluate-tool-selection.py"
    spec = importlib.util.spec_from_file_location("tool_selection_scorer", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = json.loads(
        (ROOT / "integrations" / "hermes" / "tool-selection-evals.json").read_text()
    )
    record = {
        "id": "create-codex-interactive-shell",
        "model": "primary",
        "first_tool": "create_shell",
        "arguments": {
            "profile": "codex",
            "host": "corsair-ai",
            "workdir": "/work/repo",
        },
        "transcript": "Created through ARIA.",
    }
    report = module.score([record], [cases[4]])
    assert report["ok"] is True
    record["transcript"] = "I used tmux new-session instead."
    assert module.score([record], [cases[4]])["ok"] is False
