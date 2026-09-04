"""Regression coverage for the shared Mac Claude/Codex/Pi desk wrapper."""

from __future__ import annotations

import runpy
from pathlib import Path
import sys
import urllib.error
from io import BytesIO

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "aria-local-shell"
REMOTE_SCRIPT = Path(__file__).parents[2] / "scripts" / "aria-remote-shell"
MAC_ROUTER = Path(__file__).parents[2] / "scripts" / "aria-shells-mac.sh"
CODEX_LAUNCH = Path(__file__).parents[2] / "scripts" / "aria-codex-launch"


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
    monkeypatch.setitem(globals_, "_wait_for_readiness", lambda *_a: True)

    conflict = urllib.error.HTTPError(
        "http://127.0.0.1:8200/api/v1/shells", 409, "Conflict", {}, None
    )
    monkeypatch.setattr(
        globals_["urllib"].request,
        "urlopen",
        lambda *_a, **_kw: (_ for _ in ()).throw(conflict),
    )

    calls = []
    option_calls = []
    monkeypatch.setattr(globals_["subprocess"], "call", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(
        globals_["subprocess"],
        "run",
        lambda argv, **_kwargs: option_calls.append(argv),
    )
    if tmux_env:
        monkeypatch.setenv("TMUX", tmux_env)
    else:
        monkeypatch.delenv("TMUX", raising=False)

    assert main() == 0
    assert option_calls == [
        [
            "tmux",
            "set-option",
            "-w",
            "-t",
            f"{expected_target[1:]}:",
            "window-size",
            "smallest",
        ]
    ]
    assert calls == [["tmux", expected_action, "-t", expected_target]]


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_readiness_retries_connection_refused_then_succeeds(monkeypatch):
    script = _load_script()
    globals_ = script["_wait_for_readiness"].__globals__
    responses = iter(
        [
            urllib.error.URLError("connection refused"),
            _Response(b'{"ready": false, "phase": "database", "blocked_on": "MongoDB"}'),
            _Response(b'{"ready": true, "phase": "ready"}'),
        ]
    )

    def urlopen(*_args, **_kwargs):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    ticks = iter([0.0, 0.0, 0.0, 0.1, 0.1, 0.2, 0.2, 0.3])
    monkeypatch.setattr(globals_["urllib"].request, "urlopen", urlopen)
    monkeypatch.setattr(globals_["time"], "monotonic", lambda: next(ticks))
    monkeypatch.setattr(globals_["time"], "sleep", lambda _delay: None)
    monkeypatch.setitem(globals_["os"].environ, "ARIA_STARTUP_RETRY_INITIAL_SECONDS", "0.01")

    assert script["_wait_for_readiness"]("http://127.0.0.1:8200", 10.0) is True


def test_registration_retries_503_but_fails_fast_on_auth(monkeypatch, capsys):
    script = _load_script()
    register = script["_register_shell"]
    globals_ = register.__globals__
    request = urllib.request.Request("http://127.0.0.1/shells", data=b"{}")
    responses = iter(
        [
            urllib.error.HTTPError(request.full_url, 503, "starting", {}, BytesIO(b"{}")),
            _Response(b'{"name": "claude-codex-project-deadbe"}'),
        ]
    )

    def urlopen(*_args, **_kwargs):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(globals_["urllib"].request, "urlopen", urlopen)
    monkeypatch.setattr(globals_["time"], "sleep", lambda _delay: None)
    monkeypatch.setattr(globals_["time"], "monotonic", lambda: 0.0)
    assert register(request, 10.0) == (
        True,
        {"name": "claude-codex-project-deadbe"},
    )

    unauthorized = urllib.error.HTTPError(
        request.full_url, 401, "Unauthorized", {}, BytesIO(b"bad key")
    )
    monkeypatch.setattr(
        globals_["urllib"].request,
        "urlopen",
        lambda *_a, **_kw: (_ for _ in ()).throw(unauthorized),
    )
    assert register(request, 10.0) == (False, {})
    assert "registration failed (401): bad key" in capsys.readouterr().err


def test_readiness_timeout_never_launches_tmux(monkeypatch):
    script = _load_script()
    main = script["main"]
    globals_ = main.__globals__
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "codex"])
    monkeypatch.setattr(globals_["os"], "getcwd", lambda: "/tmp/project")
    monkeypatch.setitem(globals_, "_load_key", lambda: "test-key")
    monkeypatch.setattr(globals_["Path"], "home", classmethod(lambda cls: Path("/tmp")))
    monkeypatch.setattr(globals_["Path"], "is_file", lambda self: True)
    monkeypatch.setitem(globals_, "_wait_for_readiness", lambda *_a: False)
    calls = []
    monkeypatch.setattr(globals_["subprocess"], "call", lambda argv: calls.append(argv) or 0)

    assert main() == 1
    assert calls == []


def test_remote_wrapper_consumes_aria_placement_flags():
    source = REMOTE_SCRIPT.read_text(encoding="utf-8")

    assert "--local|--no-aria|--corsair|--remote) ;;" in source
    assert '"$launcher" "${forwarded_args[@]}"' in source


def test_remote_wrapper_local_flag_stays_managed():
    source = REMOTE_SCRIPT.read_text(encoding="utf-8")

    assert "local_mode" not in source
    assert 'exec "$tool" "${forwarded_args[@]}"' not in source


def test_mac_wrapper_local_flags_remain_registered_compatibility_aliases():
    source = MAC_ROUTER.read_text(encoding="utf-8")

    assert "--local|--no-aria) ;;" in source
    assert "bypass_aria" not in source
    assert '"$HOME/.config/aria/aria-local-shell" "$tool"' in source


def test_codex_launch_shim_makes_auto_resume_opt_in_and_keeps_fresh_fallback():
    source = CODEX_LAUNCH.read_text(encoding="utf-8")

    # Auto-resume is opt-in and bounded: a rollout must not make every new
    # shell in its directory slow while Codex replays it.
    assert "ARIA_CODEX_RESUME_MAX_BYTES" in source
    assert 'ARIA_CODEX_RESUME_MAX_BYTES:-0' in source
    assert "TUI_ARGS=(--no-alt-screen -c tui.animations=false)" in source
    assert "codex resume --last" in source
    # A failed resume must always fall back to a fresh session rather than
    # leave a dead pane, however long the failure took.
    assert "starting a fresh session" in source
    assert 'exec codex "${TUI_ARGS[@]}" "${CODEX_ARGS[@]}" "$@"' in source
