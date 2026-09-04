"""Project registry harvester.

Populates the `projects` collection from *derived* signals so the registry is
never hand-maintained:

  - git repos under the configured roots (existence + last commit + branch)
  - Claude Code sessions (~/.claude/projects/*/) — authoritative cwd + mtimes
  - pi agent sessions (~/.pi/agent/sessions/*)
  - the `shells` collection (live tmux sessions + status)

Projects are keyed by canonical path (git toplevel when available). Derived
fields are overwritten on every run; human-editable fields (summary, next_steps,
status, name, tags, check_command, charter) are only set on insert so the
dashboard can edit them.

Discovery is not curation: everything found is registered, but only what looks
like a real project is registered AS one. `kind` (project/scratch/ignored)
carries that distinction so the cockpit and the steward's active set stop
treating ~/Downloads and a .worktrees checkout as work Ben cares about. Rows
matching HARVEST_IGNORE are reconciled to kind=ignored rather than skipped —
skipping would leave the 20 junk rows already in the collection untouched
forever, and S3 says vanished/rejected things go stale, never deleted.
"""

from __future__ import annotations

import asyncio
import fnmatch
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

# Provenance actor for every field this module writes (S3 `source.<field>`).
# It is also how the kind reconciler tells "the harvester guessed this" from
# "a human decided this" — the latter is never overwritten.
HARVEST_ACTOR = "project-harvester"

# Paths that are not projects, as full-path globs (`~` expanded, fnmatch
# semantics so `*` crosses `/`). Measured against the live collection on
# 2026-08-15, these 20 of 59 rows are what this kills:
#   Desktop, Documents, Downloads, Public   — ~/ inbox dirs, never code
#   venv                                    — /home/ben/venv, a virtualenv
#   vault                                   — /home/ben/Obsidian/vault, the notes
#   tmp, workspace, routetest, rt2,
#   scratchpad, ui-test-*, session-*,
#   aria-pi-{,id-,session-id-}smoke.*       — /tmp scratch and pi smoke-test dirs
#   ridge_review-*, session-*               — **/.worktrees/* agent checkouts
#   rocmfpx-decode-fusion-wt                — a *-wt worktree by naming convention
#   Dev, Development                        — the harvest ROOTS themselves; a row
#     for a parent directory swallows its children in PathIndex attribution
# A human can always override any of these by setting `kind` on the row; the
# harvester will then leave it alone (see _reconcile_kind).
HARVEST_IGNORE = [
    "~/Downloads", "~/Downloads/**",
    "~/Desktop", "~/Desktop/**",
    "~/Documents", "~/Documents/**",
    "~/Public", "~/Public/**",
    "~/Obsidian", "~/Obsidian/**",
    "~/venv", "~/venv/**",
    "~/Dev", "~/Development",
    "/tmp", "/tmp/**",
    "/var/tmp", "/var/tmp/**",
    "**/.worktrees/**",
    "**/venv/**", "**/.venv/**",
    "**/node_modules/**",
    "**/site-packages/**",
    "**/.cache/**",
]

# Basename globs. `*-smoke.*` is pi's mkstemp-suffixed smoke dirs, `session-*`
# the watchdog's worktree branches, `*-wt` the hand-made worktree convention
# used in infrastructure/.
#
# ⚠️ These match a BASENAME anywhere on the box, so they are a naming
# convention, not evidence — a real repo called `session-recorder` or
# `foo-wt` would be classified `ignored` on its name alone. Audited against
# every discovered path on 2026-08-15: nothing real is misclassified today
# (the only name-glob-only hit, `infrastructure/rocmfpx-decode-fusion-wt`, is a
# worktree that no longer exists on disk). The safety net is that this verdict
# is now RECOVERABLE: a human charter on a harvester-classified row promotes it
# to kind=project (planning.service.set_charter) instead of being silently
# stored on a row the steward never reads. Do NOT make the check structural
# ("a linked worktree is not a project") — `Development/operator-placement-manifest`
# is a live worktree of infrastructure and a kind=project row.
HARVEST_IGNORE_NAMES = [
    "*-smoke.*",
    "session-*",
    "*-wt",
    "scratchpad",
]

# What makes a discovered directory a project rather than scratch. Deliberately
# cheap and structural: a remote means someone else has seen it, three commits
# means it survived a sitting, a README/CLAUDE.md means it was explained to
# somebody. Anything else starts as `scratch` and a human can promote it.
PROJECT_MARKER_FILES = ("README.md", "README", "README.rst", "README.txt", "CLAUDE.md", "AGENTS.md")
PROJECT_MIN_COMMITS = 3


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


def _is_ignored(path: str) -> bool:
    """True if this path is inventory, not a project (HARVEST_IGNORE)."""
    p = os.path.normpath(path)
    for pattern in HARVEST_IGNORE:
        expanded = [os.path.expanduser(pattern)]
        # ARIA harvests Mac-local and Corsair-reported paths in the same
        # process. A bare `~` only expands for the Mac service account, which
        # previously let Corsair's /home/ben inbox directories become projects.
        if pattern.startswith("~/"):
            suffix = pattern[2:]
            expanded.extend(f"{home}/{suffix}" for home in ("/Users/ben", "/home/ben"))
        if any(fnmatch.fnmatchcase(p, candidate) for candidate in expanded):
            return True
    name = os.path.basename(p)
    return any(fnmatch.fnmatchcase(name, pat) for pat in HARVEST_IGNORE_NAMES)


def _looks_like_project(path: str) -> bool:
    """A git remote OR >= PROJECT_MIN_COMMITS commits OR a README/CLAUDE.md.

    Each is evidence somebody intended this directory to outlive an afternoon;
    a bare `git init` with one commit and no docs is scratch until a human says
    otherwise."""
    if _git(path, "remote"):
        return True
    count = _git(path, "rev-list", "--count", "HEAD")
    try:
        if count is not None and int(count.strip()) >= PROJECT_MIN_COMMITS:
            return True
    except ValueError:
        pass
    return any((Path(path) / marker).exists() for marker in PROJECT_MARKER_FILES)


def _desired_kind(path: str, existing: Optional[dict] = None) -> str:
    """Classify a path, skipping the git probes when the answer cannot change:
    a row already believed to be a project is never demoted (see
    _reconcile_kind), so re-running `git remote`/`rev-list` on ~40 repos every
    30 minutes would buy nothing."""
    if _is_ignored(path):
        return "ignored"
    if existing and existing.get("kind") == "project":
        return "project"
    return "project" if _looks_like_project(path) else "scratch"


def _reconcile_kind(existing: Optional[dict], desired: str) -> tuple[Optional[str], Optional[str]]:
    """Decide what `kind` the harvester may write on an existing row.

    Returns (kind_to_set, conflict_detail). The rules, in S3 terms:
      - a human-set kind is never overwritten (provenance actor != harvester);
      - an ignore-list match always wins over a previous harvester guess — that
        is how the junk rows already in the collection get reconciled;
      - otherwise the harvester only ever *upgrades* scratch -> project. It
        never demotes: a project whose README is momentarily missing must not
        silently drop out of the steward's active set.
    """
    if not existing:
        return desired, None
    current = existing.get("kind")
    actor = ((existing.get("source") or {}).get("kind") or {}).get("actor")
    human_owned = current is not None and actor != HARVEST_ACTOR
    if human_owned:
        # Propose, don't clobber — but only for the one combination that costs
        # something: a human-blessed project living on an ignored path is in the
        # active set, so the steward will spend budget in a directory that /tmp
        # or a pruned worktree can delete out from under it.
        if desired == "ignored" and current == "project":
            return None, f"kind=project was set by hand on an ignored path ({existing.get('path')})"
        return None, None
    if current is None:
        return desired, None
    if desired == "ignored" and current != "ignored":
        return "ignored", None
    if current == "scratch" and desired == "project":
        return "project", None
    return None, None


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


def _discover(roots: list[str]) -> tuple[list[str], dict[str, dict], dict[str, dict]]:
    """The filesystem half of a harvest: os.walk over the roots, a `git
    rev-parse` per repo, and the Claude/pi session-file reads.

    Sync on purpose and called via `asyncio.to_thread` -- see `harvest`.
    """
    return (
        _find_git_repos(roots + EXTRA_REPO_ROOTS),
        _gather_claude(),
        _gather_pi(),
    )


def _aggregate(
    canon_paths: set[str],
    claude: dict[str, dict],
    pi: dict[str, dict],
    shells: dict[str, dict],
) -> dict[str, dict]:
    """Collapse discovered paths into one record per slug.

    Sync on purpose: it runs two `git` subprocesses per repo with a 10 s
    timeout each, which is the bulk of a harvest's wall-clock. Called via
    `asyncio.to_thread` so that cost never lands on the event loop.
    """
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
    return agg


async def harvest(db, roots: Optional[list[str]] = None) -> dict:
    """Discover + upsert projects. Returns a summary dict."""
    roots = roots or DEFAULT_ROOTS
    # Discovery and aggregation are sync (os.walk, git subprocesses, jsonl
    # reads) and used to run directly on the event loop, stalling every HTTP
    # request, SSE stream and watchdog tick for the duration of a harvest --
    # seconds across ~40 repos, every 30 minutes. Both halves now run in a
    # thread; only the DB work below stays on the loop.
    repos, claude, pi = await asyncio.to_thread(_discover, roots)
    shells = await _gather_shells(db)

    # Union of all canonical paths seen.
    canon_paths: set[str] = set(repos) | set(claude) | set(pi) | set(shells)

    now = _utcnow()
    active_cutoff = now - timedelta(days=ACTIVE_WINDOW_DAYS)

    # Aggregate per slug so moved/duplicated repos (same basename, different
    # path) collapse into one project with all paths recorded.
    agg = await asyncio.to_thread(_aggregate, canon_paths, claude, pi, shells)

    upserts = 0
    merged = 0
    ignored_rows = 0
    conflicts = 0
    for slug, rec in agg.items():
        last_activity = max(rec["activity"]) if rec["activity"] else None
        is_active = bool(last_activity and last_activity >= active_cutoff)
        primary_path = (rec["git"] or {}).get("path") or sorted(rec["paths"])[0]
        git_info = None
        if rec["git"]:
            git_info = {k: v for k, v in rec["git"].items() if k != "path"}

        # Match an EXISTING project by the path it claims before falling back to
        # the directory-derived slug.
        #
        # Without this, a hand-created project is silently shadowed by a
        # harvested twin: "ARIA" (slug `aria`, created by hand with a real
        # summary, claiming ~/Development/ProjectAria via relevant_paths) got a
        # duplicate "ProjectAria" (slug from the directory name) with an empty
        # summary. Both then claimed the same root, splitting that project's
        # memories, cockpit rollups and path attribution across two rows —
        # and whichever won a tie depended on iteration order.
        #
        # Slug stays the fallback so genuinely new directories still register.
        key = {"slug": slug}
        existing = await db.projects.find_one(
            {
                "slug": {"$ne": slug},
                "$or": [
                    {"path": primary_path},
                    {"relevant_paths": primary_path},
                ],
            },
            {"_id": 1, "slug": 1, "kind": 1, "relevant_paths": 1, "source": 1, "path": 1},
        )
        # `current` is the row as it stands, so this run can MERGE rather than
        # overwrite. The claim lookup above already read it when it hit, so only
        # the ordinary same-slug case costs a second query.
        if existing:
            key = {"_id": existing["_id"]}
            current = existing
            merged += 1
            logger.debug(
                "harvest: %s already claims %s — refreshing it instead of "
                "creating duplicate slug '%s'",
                existing.get("slug"), primary_path, slug,
            )
        else:
            current = await db.projects.find_one(
                key, {"kind": 1, "relevant_paths": 1, "source": 1, "path": 1, "slug": 1}
            )

        desired_kind = _desired_kind(primary_path, current)
        kind_to_set, kind_conflict = _reconcile_kind(current, desired_kind)
        if desired_kind == "ignored":
            ignored_rows += 1
        if kind_conflict:
            conflicts += 1
            await _propose_kind_review(db, current.get("slug") or slug, kind_conflict)

        # relevant_paths is human-editable (ProjectUpdateRequest exposes it) and
        # was being flattened to the discovered set on every 30-minute tick, so
        # any path Ben added by hand survived at most half an hour. Union
        # instead: discovery adds, it does not decide. A path that disappears
        # from disk stays listed (S3: vanished things go stale, not deleted).
        discovered_paths = set(rec["paths"])
        known_paths = set((current or {}).get("relevant_paths") or [])
        set_fields: dict = {
            "path": primary_path,
            "relevant_paths": sorted(discovered_paths | known_paths),
            "last_activity_at": last_activity,
            "activity_status": "active" if is_active else "idle",
            "sources": rec["sources"],
            "git": git_info,
            "harvested_at": now,
            "updated_at": now,
        }
        if kind_to_set is not None:
            set_fields["kind"] = kind_to_set
            set_fields["source.kind"] = {"actor": HARVEST_ACTOR, "at": now}

        update: dict = {
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
                "check_command": None,
                # Human-owned (charter) and worker-owned-by-someone-else
                # (steward) fields exist from insert so their absence never
                # reads as "not yet harvested". The harvester writes neither
                # again, ever.
                #
                # ⚠️ `steward` is an EMPTY DOCUMENT, not null. The steward writes
                # its bookkeeping with dotted paths and MongoDB cannot create a
                # field under a null parent — "Cannot create field
                # 'no_progress_streak' in element {steward: null}" (live mongod
                # 8.2.0). A row inserted with null here was permanently
                # unusable by the steward; a MISSING parent auto-creates, which
                # is the only reason the 59 pre-existing rows never hit it.
                # `charter` stays null: it is only ever written wholesale.
                "charter": None,
                "steward": {},
                "recent_activity": [],
                "created_at": now,
            },
            "$set": set_fields,
        }
        if last_activity is not None:
            # `last_signal_at` was null on all 59 rows, which broke every
            # staleness check downstream: the only other timestamp, updated_at,
            # is bumped by this worker every 30 minutes whether or not anything
            # happened, so nothing could ever look stale. $max (not $set) so a
            # fresher signal from append_project_activity is never walked back.
            update["$max"] = {"last_signal_at": last_activity}

        await db.projects.update_one(key, update, upsert=True)
        upserts += 1

    return {
        "discovered": len(canon_paths),
        "slugs": len(agg),
        "upserted": upserts,
        # Harvested paths that resolved to an existing project rather than
        # minting a duplicate slug.
        "matched_existing": merged,
        "repos": len(repos),
        "ignored": ignored_rows,
        "kind_conflicts": conflicts,
    }


async def _propose_kind_review(db, slug: str, detail: str) -> None:
    """Surface a human/harvester `kind` contradiction instead of overwriting it.

    Import is local because harvest() must keep working if the shared-services
    review surface is unavailable — classification is convenience, not safety."""
    try:
        from aria.shared.review import add_review_item

        await add_review_item(db, kind="project_kind_conflict", subject=slug, detail=detail,
                              source=HARVEST_ACTOR)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("harvest: could not file kind conflict for %s: %s", slug, exc)


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
