#!/usr/bin/env python3
"""Hermes pre-tool hook enforcing ARIA-owned coding and shell launches.

Hermes may inspect repositories locally.  Mutating repository work belongs to
an ARIA coding session, while interactive Claude/Codex/Pi shells belong to the
ARIA shell registry.  Keeping both paths here prevents an agent from creating
an invisible tmux session merely because a terminal invocation omitted a cwd.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any


MUTATING = {
    "write_file",
    "patch",
    "terminal",
    "edit_file",
    "apply_patch",
    "str_replace",
    "create_file",
    "shell",
    "bash",
    "code_execution",
    "run_command",
}
ALLOW_PREFIXES = (
    "/tmp/",
    "/var/tmp/",
    "/Users/ben/Services/data/hermes-home/",
    "/dev/",
    "/proc/",
)
_INTERACTIVE_LAUNCH = re.compile(
    r"(?:^|[;&|]\s*|\bexec\s+)(?:tmux\s+(?:new|new-session)\b[^\n]*?\s+)?"
    r"(?:claude|codex|pi)(?:\s|$)",
    re.IGNORECASE,
)
_TMUX_CREATE = re.compile(r"(?:^|[;&|]\s*)tmux\s+(?:new|new-session)\b", re.IGNORECASE)
_REPOSITORY_MUTATION = re.compile(
    r"(?:^|[;&|]\s*|\bsudo\s+)"
    r"(?:git\s+(?:add|am|apply|bisect|branch\s+(?:-[dDmM]|--delete|--move)|"
    r"checkout|cherry-pick|clean|commit|merge|mv|pull|push|rebase|reset|restore|"
    r"revert|rm|stash|switch|tag)|apply_patch\b|patch\s+-p\d+\b|sed\s+[^;&|]*\s-i\b)",
    re.IGNORECASE,
)


def _in_git_repo(path: str) -> bool:
    try:
        directory = path if os.path.isdir(path) else os.path.dirname(path) or "."
        result = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


def _candidate_paths(args: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key, value in args.items():
        if not isinstance(value, str) or not value:
            continue
        lowered = key.lower()
        if any(part in lowered for part in ("path", "file", "dir", "workspace", "cwd", "target")):
            paths.append(value)
        if lowered in {"command", "cmd", "script", "code"}:
            for token in value.replace("'", " ").replace('"', " ").split():
                if token.startswith("/") and len(token) > 1:
                    paths.append(token)
    return paths


def _command(args: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script", "code"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return ""


def _block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print("{}")
        return

    tool = str(payload.get("tool_name") or "").lower()
    if tool not in MUTATING:
        print("{}")
        return

    args = payload.get("tool_input") or payload.get("args") or {}
    if not isinstance(args, dict):
        _block("BLOCKED: malformed mutating-tool arguments; do not bypass ARIA routing.")
        return

    command = _command(args)
    if _INTERACTIVE_LAUNCH.search(command) or _TMUX_CREATE.search(command):
        _block(
            "BLOCKED: interactive shells must be created through ARIA so they are visible, "
            "host-addressable, and recoverable. Call the aria MCP create_shell tool with "
            "profile='claude', 'codex', 'pi', or 'shell', plus workdir and host when needed. "
            "Do not create tmux manually. If the request is a self-contained coding task "
            "rather than an interactive shell, call create_coding_session instead."
        )
        return

    if _REPOSITORY_MUTATION.search(command):
        _block(
            "BLOCKED: repository-changing terminal commands must run in an ARIA "
            "coding session so the work is watched, checkpointed, and steerable. "
            "Call create_coding_session with the repository workspace and complete "
            "task. Read-only git/status/diff/log inspection remains allowed."
        )
        return

    paths = _candidate_paths(args)
    for path in paths:
        absolute = os.path.abspath(os.path.expanduser(path))
        if absolute.startswith(ALLOW_PREFIXES):
            continue
        if _in_git_repo(absolute):
            _block(
                "BLOCKED: repository mutations belong to an ARIA coding session. "
                f"Repository target: {absolute}. Call aria create_coding_session with the "
                "absolute workspace, a complete task prompt, an explicit backend/profile "
                "when the user requested one, and a host only when placement matters. "
                "If ARIA is unavailable, report that and stop; do not mutate locally."
            )
            return

    print("{}")


if __name__ == "__main__":
    main()
