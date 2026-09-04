"""CLI contract tests for watched-shell identifiers."""

from __future__ import annotations

from click.testing import CliRunner
from rich.console import Console

from aria_cli import main


CANONICAL_NAME = "claude-claude-red5090-034654"
SHORT_NAME = "claude-red5090-034654"


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    @property
    def content(self):
        return b"{}" if self._data else b""


class RecordingClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return FakeResponse(self.responses.get((method, path), {}))


def test_shells_list_displays_canonical_addressable_name(monkeypatch):
    client = RecordingClient(
        {
            ("GET", "/shells/overview"): {
                "shells": [
                    {
                        "name": CANONICAL_NAME,
                        "short_name": SHORT_NAME,
                        "status": "active",
                        "activity_state": "working",
                        "connectivity_state": "online",
                        "host": "corsair-ai",
                        "project_dir": "/home/ben/Development/infrastructure/red5090",
                        "line_count": 5540,
                        "last_activity_at": "2026-09-01T23:41:17Z",
                    }
                ]
            }
        }
    )
    monkeypatch.setattr(main, "AriaClient", lambda: client)
    monkeypatch.setattr(main, "console", Console(width=240))

    result = CliRunner().invoke(main.cli, ["shells", "list"])

    assert result.exit_code == 0, result.output
    assert CANONICAL_NAME in result.output
    assert "corsair-ai" in result.output
    assert client.calls == [("GET", "/shells/overview", {"params": {}})]


def test_shell_commands_use_displayed_canonical_name_unchanged(monkeypatch):
    client = RecordingClient(
        {
            ("GET", f"/shells/{CANONICAL_NAME}"): {
                "name": CANONICAL_NAME,
                "short_name": SHORT_NAME,
            },
            ("GET", f"/shells/{CANONICAL_NAME}/events"): {"events": []},
            ("DELETE", f"/shells/{CANONICAL_NAME}"): {},
        }
    )
    monkeypatch.setattr(main, "AriaClient", lambda: client)
    runner = CliRunner()

    for args in (
        ["shells", "info", CANONICAL_NAME],
        ["shells", "tail", CANONICAL_NAME],
        ["shells", "rm", CANONICAL_NAME],
    ):
        result = runner.invoke(main.cli, args)
        assert result.exit_code == 0, result.output

    assert [(method, path) for method, path, _kwargs in client.calls] == [
        ("GET", f"/shells/{CANONICAL_NAME}"),
        ("GET", f"/shells/{CANONICAL_NAME}/events"),
        ("DELETE", f"/shells/{CANONICAL_NAME}"),
    ]
