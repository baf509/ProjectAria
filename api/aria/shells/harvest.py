"""Project registry harvester.

Populates the `projects` collection from *derived* signals so the registry is
never hand-maintained:

  - git repos under the configured roots (existence + last commit + branch)
  - Claude Code sessions (~/.claude/projects/*/) — authoritative cwd + mtimes
  - pi agent sessions (~/.pi/agent/sessions/*)
  - the `shells` collection (live tmux sessions + status)

Projects are keyed by canonical path (git toplevel when available). Derived
fields are overwritten on every run; human-editable fields (summary, next_steps,
status, name, tags) are only set on insert so the dashboard can edit them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_ROOTS = ["/home/ben/Development"]
EXTRA_REPO_ROOTS = ["/home/ben/Hermes", "/home/ben/pes17-staging", "/home/ben/pes17-base-staging"]
PRUNE_DIRS = {"node_modules", ".venv", "venv", ".git", "__pycache__", "Archive", ".cache", "site-packages"}
CLAUDE_PROJECTS = Path("/home/ben/.claude/projects")
PI_SESSIONS = Path("/home/ben/.pi/agent/sessions")
ACTIVE_WINDOW_DAYS = 7


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mtime(p: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _git(path: str, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _canonical(path: str) -> Optional[str]:
    """Resolve a path to its git toplevel, else realpath. None if it's gone."""
    if not path or not os.path.isdir(path):
        return None
    top = _git(path, "rev-parse", "--show-toplevel")
    return top or os.path.realpath(path)


def _find_git_repos(roots: list[str], max_depth: int = 3) -> list[str]:
    repos: set[str] = set()
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        base_depth = len(base.parts)
        for dirpath, dirnames, _files in os.walk(base):
            depth = len(Path(dirpath).parts) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
            if (Path(dirpath) / ".git").exists():
                repos.add(dirpath)
                dirnames[:] = []  # don't descend into a repo's subdirs
    return sorted(repos)


def _claude_cwd(project_dir: Path) -> Optional[str]:
    """Read the authoritative cwd from the newest session jsonl in a dir."""
    sessions = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for sess in sessions[:3]:
        try:
            with sess.open("r", errors="ignore") as fh:
                for _i, line in zip(range(80), fh):
                    if '"cwd"' in line:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        cwd = obj.get("cwd")
                        if cwd:
                            return cwd
        except OSError:
            continue
    return None


def _gather_claude() -> dict[str, dict]:
    """canonical_path -> {last_activity, sessions} from Claude project dirs."""
    out: dict[str, dict] = {}
    if not CLAUDE_PROJECTS.is_dir():
        return out
    for pdir in CLAUDE_PROJECTS.iterdir():
        if not pdir.is_dir():
            continue
        jsonls = list(pdir.glob("*.jsonl"))
        if not jsonls:
            continue
        cwd = _claude_cwd(pdir)
        canon = _canonical(cwd) if cwd else None
        if not canon:
            continue
        last = max((_mtime(j) for j in jsonls if _mtime(j)), default=None)
        rec = out.setdefault(canon, {"last_activity": None, "sessions": 0})
        rec["sessions"] += len(jsonls)
        if last and (rec["last_activity"] is None or last > rec["last_activity"]):
            rec["last_activity"] = last
    return out


def _decode_session_dirname(name: str) -> Optional[str]:
    """pi/claude style '--home-ben-Dev-aiPanel--' -> '/home/ben/Dev/aiPanel'."""
    s = name.strip("-")
    if not s:
        return None
    return "/" + s.replace("-", "/")


def _gather_pi() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not PI_SESSIONS.is_dir():
        return out
    for sdir in PI_SESSIONS.iterdir():
        if not sdir.is_dir():
            continue
        decoded = _decode_session_dirname(sdir.name)
        canon = _canonical(decoded) if decoded else None
        if not canon:
            continue
        last = _mtime(sdir)
        files = [p for p in sdir.iterdir() if p.is_file()]
        rec = out.setdefault(canon, {"last_activity": None, "sessions": 0})
        rec["sessions"] += len(files)
        if last and (rec["last_activity"] is None or last > rec["last_activity"]):
            rec["last_activity"] = last
    return out


async def _gather_shells(db) -> dict[str, dict]:
    out: dict[str, dict] = {}
    async for sh in db.shells.find({}, {"name": 1, "project_dir": 1, "status": 1, "last_activity_at": 1}):
        canon = _canonical(sh.get("project_dir", "")) or sh.get("project_dir")
        if not canon:
            continue
        rec = out.setdefault(canon, {"shells": [], "last_activity": None})
        rec["shells"].append({"name": sh.get("name"), "status": sh.get("status")})
        la = sh.get("last_activity_at")
        if la and la.tzinfo is None:
            la = la.replace(tzinfo=timezone.utc)
        if la and (rec["last_activity"] is None or la > rec["last_activity"]):
            rec["last_activity"] = la
    return out


async def harvest(db, roots: Optional[list[str]] = None) -> dict:
    """Discover + upsert projects. Returns a summary dict."""
    roots = roots or DEFAULT_ROOTS
    repos = _find_git_repos(roots + EXTRA_REPO_ROOTS)
    claude = _gather_claude()
    pi = _gather_pi()
    shells = await _gather_shells(db)

    # Union of all canonical paths seen.
    canon_paths: set[str] = set(repos) | set(claude) | set(pi) | set(shells)

    now = _utcnow()
    active_cutoff = now - timedelta(days=ACTIVE_WINDOW_DAYS)

    # Aggregate per slug so moved/duplicated repos (same basename, different
    # path) collapse into one project with all paths recorded.
    agg: dict[str, dict] = {}
    for path in sorted(canon_paths):
        if path in ("/home/ben", "/", "/home"):  # skip non-projects
            continue
        slug = os.path.basename(path.rstrip("/")) or path
        rec = agg.setdefault(slug, {
            "paths": set(), "activity": [], "sources": [],
            "git": None, "git_commit_at": None,
        })
        rec["paths"].add(path)

        c = claude.get(path)
        if c:
            rec["sources"].append({"type": "claude", "path": path, "sessions": c["sessions"], "last": c["last_activity"]})
            if c["last_activity"]:
                rec["activity"].append(c["last_activity"])

        p = pi.get(path)
        if p:
            rec["sources"].append({"type": "pi", "path": path, "sessions": p["sessions"], "last": p["last_activity"]})
            if p["last_activity"]:
                rec["activity"].append(p["last_activity"])

        s = shells.get(path)
        if s:
            rec["sources"].append({"type": "shells", "path": path, "shells": s["shells"], "last": s["last_activity"]})
            if s["last_activity"]:
                rec["activity"].append(s["last_activity"])

        if (Path(path) / ".git").exists():
            branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
            last_commit = _git(path, "log", "-1", "--format=%cI\x1f%s")
            commit_at = commit_subject = None
            if last_commit and "\x1f" in last_commit:
                ciso, commit_subject = last_commit.split("\x1f", 1)
                try:
                    commit_at = datetime.fromisoformat(ciso).astimezone(timezone.utc)
                except ValueError:
                    commit_at = None
            # Keep the most-recently-committed repo as this slug's canonical git.
            if commit_at and (rec["git_commit_at"] is None or commit_at > rec["git_commit_at"]):
                rec["git"] = {"branch": branch, "last_commit_at": commit_at,
                              "last_commit_subject": commit_subject, "path": path}
                rec["git_commit_at"] = commit_at
            if commit_at:
                rec["activity"].append(commit_at)

    upserts = 0
    for slug, rec in agg.items():
        last_activity = max(rec["activity"]) if rec["activity"] else None
        is_active = bool(last_activity and last_activity >= active_cutoff)
        primary_path = (rec["git"] or {}).get("path") or sorted(rec["paths"])[0]
        git_info = None
        if rec["git"]:
            git_info = {k: v for k, v in rec["git"].items() if k != "path"}

        await db.projects.update_one(
            {"slug": slug},
            {
                "$setOnInsert": {
                    "slug": slug,
                    "name": slug,
                    "summary": "",
                    "next_steps": [],
                    # Human lifecycle status — always a valid ProjectStatus on
                    # insert so PlanningService can deserialize the doc. Machine
                    # active/idle lives in `activity_status` below. Dashboard owns
                    # this field thereafter (only set on insert).
                    "status": "active",
                    "tags": [],
                    "recent_activity": [],
                    "created_at": now,
                },
                "$set": {
                    "path": primary_path,
                    "relevant_paths": sorted(rec["paths"]),
                    "last_activity_at": last_activity,
                    "activity_status": "active" if is_active else "idle",
                    "sources": rec["sources"],
                    "git": git_info,
                    "harvested_at": now,
                    "updated_at": now,
                },
            },
            upsert=True,
        )
        upserts += 1

    return {"discovered": len(canon_paths), "slugs": len(agg), "upserted": upserts, "repos": len(repos)}


class ProjectHarvestWorker:
    """Periodically refresh the project registry from derived signals."""

    def __init__(self, db, interval_minutes: int = 30):
        self.db = db
        self.interval = max(60, int(interval_minutes) * 60)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="projects.harvest")
        logger.info("project harvest worker started (every %ds)", self.interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                summary = await harvest(self.db)
                logger.info("project harvest: %s", summary)
            except Exception as exc:  # pragma: no cover
                logger.warning("project harvest tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
