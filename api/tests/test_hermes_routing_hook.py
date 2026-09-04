from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


HOOK = Path(__file__).parents[2] / "integrations" / "hermes" / "route-coding-to-aria.py"


def _run(tool_name: str, tool_input: dict) -> dict:
    completed = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": tool_name, "tool_input": tool_input}),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_blocks_unwatched_coding_cli_without_a_path():
    result = _run("terminal", {"command": "codex --yolo"})
    assert result["decision"] == "block"
    assert "create_shell" in result["reason"]


def test_blocks_manual_tmux_creation():
    result = _run("terminal", {"command": "tmux new-session -d -s worker zsh"})
    assert result["decision"] == "block"
    assert "Do not create tmux manually" in result["reason"]


def test_blocks_repository_mutation_by_cwd():
    result = _run("terminal", {"command": "git commit -am fix", "cwd": str(HOOK.parent)})
    assert result["decision"] == "block"
    assert "create_coding_session" in result["reason"]


def test_blocks_relative_repository_mutation_without_cwd():
    result = _run("terminal", {"command": "git add . && git commit -m fix"})
    assert result["decision"] == "block"
    assert "create_coding_session" in result["reason"]


def test_allows_read_only_git_diagnostic():
    assert _run("terminal", {"command": "git status --short"}) == {}


def test_allows_read_only_tool_and_tmp_write():
    assert _run("read_file", {"path": str(HOOK)}) == {}
    assert _run("write_file", {"path": "/tmp/note.txt", "content": "ok"}) == {}
