"""
ARIA - Git Sensor

Purpose: Monitor git repositories for uncommitted changes, stale branches,
recent commits, and other activity signals.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from aria.awareness.base import BaseSensor, Observation
from aria.config import settings

logger = logging.getLogger(__name__)


class GitSensor(BaseSensor):
    """Watches git repositories for activity and state changes."""

    name = "git"
    category = "git"

    def __init__(self, watch_dirs: Optional[list[str]] = None):
        self.watch_dirs = watch_dirs or [settings.coding_default_workspace]
        self._last_heads: dict[str, str] = {}  # repo_path -> last HEAD sha

    def is_available(self) -> bool:
        import shutil
        return shutil.which("git") is not None

    async def poll(self) -> list[Observation]:
        observations = []
        for repo_dir in self.watch_dirs:
            repo_dir = os.path.expanduser(repo_dir)
            if not os.path.isdir(os.path.join(repo_dir, ".git")):
                continue
            try:
                obs = await self._poll_repo(repo_dir)
                observations.extend(obs)
            except Exception as e:
                logger.warning("GitSensor error for %s: %s", repo_dir, e)
        return observations

    async def _poll_repo(self, repo_dir: str) -> list[Observation]:
        observations = []
        repo_name = os.path.basename(repo_dir)

        # 1. Check for uncommitted changes
        status = await self._run_git(repo_dir, "status", "--porcelain")
        if status is not None:
            lines = [l for l in status.strip().splitlines() if l.strip()]
            if lines:
                modified = sum(1 for l in lines if l[:2].strip() in ("M", "MM", "AM"))
                untracked = sum(1 for l in lines if l.startswith("??"))
                staged = sum(1 for l in lines if l[0] in ("A", "M", "D", "R") and l[0] != "?")
                parts = []
                if modified:
                    parts.append(f"{modified} modified")
                if untracked:
                    parts.append(f"{untracked} untracked")
                if staged:
                    parts.append(f"{staged} staged")
                summary = f"{repo_name}: {', '.join(parts)} file(s)"
                observations.append(Observation(
                    sensor=self.name,
                    category=self.category,
                    event_type="uncommitted_changes",
                    summary=summary,
                    detail=status[:2000],
                    severity="info",
                    tags=[repo_name],
                ))

        # 2. Check current branch
        branch = await self._run_git(repo_dir, "branch", "--show-current")
        if branch:
            branch = branch.strip()

        # 3. Detect new commits since last poll
        head = await self._run_git(repo_dir, "rev-parse", "HEAD")
        if head:
            head = head.strip()
            last_head = self._last_heads.get(repo_dir)
            if last_head and head != last_head:
                # New commit(s) detected
                log = await self._run_git(
                    repo_dir, "log", "--oneline", f"{last_head}..{head}", "--max-count=10"
                )
                count = len(log.strip().splitlines()) if log else 0
                observations.append(Observation(
                    sensor=self.name,
                    category=self.category,
                    event_type="new_commits",
                    summary=f"{repo_name}/{branch}: {count} new commit(s)",
                    detail=log[:2000] if log else None,
                    severity="info",
                    tags=[repo_name, branch or ""],
                ))
            self._last_heads[repo_dir] = head

        # 4. Check for stale branch (no commits in >3 days on current branch)
        if branch and branch not in ("main", "master"):
            last_commit_ts = await self._run_git(
                repo_dir, "log", "-1", "--format=%ct"
            )
            if last_commit_ts:
                try:
                    ts = int(last_commit_ts.strip())
                    age_hours = (datetime.now(timezone.utc).timestamp() - ts) / 3600
                    if age_hours > 72:
                        observations.append(Observation(
                            sensor=self.name,
                            category=self.category,
                            event_type="stale_branch",
                            summary=f"{repo_name}: branch '{branch}' has no commits in {int(age_hours)}h",
                            severity="notice",
                            tags=[repo_name, branch],
                        ))
                except ValueError:
                    pass

        return observations

    async def _run_git(self, cwd: str, *args: str) -> Optional[str]:
        """Run a git command and return stdout, or None on error."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                return None
            return stdout.decode("utf-8", errors="replace")
        except Exception:
            return None
