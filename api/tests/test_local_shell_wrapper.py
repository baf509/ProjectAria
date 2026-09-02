"""Regression coverage for the shared Mac Claude/Codex/Pi desk wrapper."""

from __future__ import annotations

import runpy
from pathlib import Path
import sys
import urllib.error

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "aria-local-shell"


def _load_script() -> dict:
    return runpy.run_path(str(SCRIPT), run_name="aria_local_shell")


@pytest.mark.parametrize(
    ("request_name", "expected"),
    [
        ("claude-project-deadbe", "claude-project-deadbe"),
        ("codex-project-deadbe", "claude-codex-project-deadbe"),
        ("pi-project-deadbe", "claude-pi-project-deadbe"),
    ],
)
def test_canonical_shell_name_is_idempotent_for_every_desk_tool(request_name, expected):
    script = _load_script()
    assert script["_canonical_shell_name"](request_name) == expected


@pytest.mark.parametrize(
    ("tool", "expected_target"),
    [
        ("claude", "=claude-project-deadbe"),
        ("codex", "=claude-codex-project-deadbe"),
        ("pi", "=claude-pi-project-deadbe"),
    ],
)
@pytest.mark.parametrize(
    ("tmux_env", "expected_action"),
    [
        (None, "attach-session"),
        ("/tmp/tmux-501/default,1,0", "switch-client"),
    ],
)
def test_repeat_launch_reattaches_to_existing_shell(
    monkeypatch, tool, expected_target, tmux_env, expected_action
):
    script = _load_script()
    main = script["main"]
    globals_ = main.__globals__

    monkeypatch.setattr(sys, "argv", [str(SCRIPT), tool])
    monkeypatch.setattr(globals_["os"], "getcwd", lambda: "/tmp/project")
    monkeypatch.setattr(
        globals_["hashlib"],
        "sha256",
        lambda _value: type("Digest", (), {"hexdigest": lambda self: "deadbeef"})(),
    )
    monkeypatch.setitem(globals_, "_load_key", lambda: "test-key")
    monkeypatch.setattr(
        globals_["Path"],
        "home",
        classmethod(lambda cls: Path("/tmp")),
    )
    monkeypatch.setattr(globals_["Path"], "is_file", lambda self: True)

    conflict = urllib.error.HTTPError(
        "http://127.0.0.1:8200/api/v1/shells", 409, "Conflict", {}, None
    )
    monkeypatch.setattr(
        globals_["urllib"].request,
        "urlopen",
        lambda *_a, **_kw: (_ for _ in ()).throw(conflict),
    )

    calls = []
    monkeypatch.setattr(globals_["subprocess"], "call", lambda argv: calls.append(argv) or 0)
    if tmux_env:
        monkeypatch.setenv("TMUX", tmux_env)
    else:
        monkeypatch.delenv("TMUX", raising=False)

    assert main() == 0
    assert calls == [["tmux", expected_action, "-t", expected_target]]
