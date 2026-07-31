"""
ARIA - Git Worktree Provisioning

Purpose: Give a coding session its own isolated git worktree instead of
running directly in a shared repo checkout, so concurrent sessions (or a
session running alongside a human editing the same repo) never collide on
working-directory state or branch checkout. Mirrors the isolation pattern
Claude Code's own Workflow tool already uses (`isolation: "worktree"`).

Called from CodingSessionManager.start_session() when a caller passes
create_worktree=True with `workspace` pointing at a source repo (not a
worktree destination). If that path isn't a git repo yet, it's initialized
first — a worktree needs at least one commit to branch from, so a fresh
`git init` gets an empty initial commit before the worktree is added.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Optional


class WorktreeError(Exception):
    """Raised when a git operation needed to provision a worktree fails."""


def _run(args: list[str], cwd: str) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WorktreeError(
            f"`{' '.join(args)}` in {cwd} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-")
    return slug or "session"


def _is_git_repo(path: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path, capture_output=True, text=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _ignore_worktrees_dir(repo_path: str) -> None:
    """Hide .worktrees/ from `git status` in the main checkout via the
    repo-local exclude file — not .gitignore, so this never dirties the
    user's own tracked files or creates a commit in their history."""
    exclude_path = os.path.join(repo_path, ".git", "info", "exclude")
    try:
        existing = ""
        if os.path.isfile(exclude_path):
            with open(exclude_path, "r", encoding="utf-8") as f:
                existing = f.read()
        if ".worktrees/" not in existing:
            os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
            with open(exclude_path, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(".worktrees/\n")
    except OSError:
        pass  # Best-effort — a missing exclude entry is cosmetic, not fatal.


def ensure_repo(repo_path: str) -> bool:
    """Ensure repo_path exists and is a git repo with a valid HEAD.

    Returns True if a repo was freshly initialized here, False if one
    already existed.
    """
    os.makedirs(repo_path, exist_ok=True)
    if _is_git_repo(repo_path):
        return False
    _run(["git", "init"], cwd=repo_path)
    # A brand-new repo has no commits, so `git worktree add -b <branch>` has
    # nothing to branch from — give it one, empty, before returning.
    _run(
        [
            "git", "-c", "user.name=ARIA", "-c", "user.email=aria@localhost",
            "commit", "--allow-empty", "-m", "Initial commit (auto-created by ARIA)",
        ],
        cwd=repo_path,
    )
    return True


def create_worktree(repo_path: str, name: Optional[str] = None) -> tuple[str, str, bool]:
    """Ensure repo_path is a usable git repo (init if needed), then add a new
    worktree + branch under <repo_path>/.worktrees/<slug>.

    Returns (worktree_path, branch_name, repo_was_initialized).
    """
    repo_path = os.path.abspath(repo_path)
    initialized = ensure_repo(repo_path)
    _ignore_worktrees_dir(repo_path)

    base_slug = _slugify(name) if name else "session"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = f"{base_slug}-{stamp}-{uuid.uuid4().hex[:6]}"
    worktree_path = os.path.join(repo_path, ".worktrees", slug)
    branch_name = f"aria/{slug}"

    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    _run(["git", "worktree", "add", worktree_path, "-b", branch_name], cwd=repo_path)
    return worktree_path, branch_name, initialized
