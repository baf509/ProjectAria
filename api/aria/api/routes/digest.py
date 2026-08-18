"""
ARIA - Project Switcher & Per-Project Cockpit read models (Coherence C4)

Purpose: the grounding surface. `/projects/overview` answers "where is
everything, and what needs me?" (all projects ranked by attention);
`/projects/{ident}/cockpit` is the focused per-project aggregate (git, agents,
tasks, verification, what-changed memories, alerts, budget). Pure read models
over existing collections — no new storage beyond the fixed-_id active-project
doc.

Related Spec Sections:
- vault/ProjectAria/Design/ARCHITECTURE.md (Coherence C4) (Project Cockpit)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_db, get_planning_service, get_shell_service
from aria.config import settings
from aria.db.usage import UsageRepo
from aria.planning.models import Project
from aria.planning.service import PlanningService
from aria.shells.service import ShellService

logger = logging.getLogger(__name__)

router = APIRouter()

_STALE_TASK_DAYS = 14


# ------------------------------------------------------------------ helpers

def _norm_path(p: Optional[str]) -> Optional[str]:
    return p.rstrip("/") if p else None


def project_roots(project: Project) -> list[str]:
    """The filesystem paths that identify a project — the join key the whole
    cockpit filters on (shells.project_dir, sessions.workspace,
    memories.source.repo all carry paths, not project ids)."""
    roots = []
    for p in [project.path, *project.relevant_paths]:
        n = _norm_path(p)
        if n and n not in roots:
            roots.append(n)
    return roots


def path_in_project(candidate: Optional[str], roots: list[str]) -> bool:
    c = _norm_path(candidate)
    if not c:
        return False
    return any(c == r or c.startswith(r + "/") for r in roots)


class PathIndex:
    """Most-specific-project ownership over filesystem paths.

    A path belongs to the project owning the LONGEST matching root — plain
    prefix matching let a coarse parent project (e.g. the harvested row for
    ~/Development itself) swallow every child project's shells, sessions,
    memories and alerts, and top the switcher ranking with other projects'
    activity."""

    def __init__(self, projects: list[Project]):
        self._entries: list[tuple[str, str]] = []
        for p in projects:
            for r in project_roots(p):
                self._entries.append((r, p.id))
        self._sort_entries()

    def _sort_entries(self) -> None:
        self._entries.sort(key=lambda e: len(e[0]), reverse=True)

    @classmethod
    def from_docs(cls, docs: list[dict], *, value: str = "slug") -> "PathIndex":
        """The same index over raw `db.projects` rows, keyed on any field.

        For callers that need path→project outside the cockpit and have no
        Project models to hand — notifications/service.py resolves an alert's
        `project_slug` this way. It exists so that attribution has ONE rule:
        the alert path and the cockpit path disagreeing about which project owns
        a directory is how an alert ends up filed where nobody looks."""
        index = cls([])
        for doc in docs:
            key = doc.get(value)
            if not key:
                continue
            for raw in [doc.get("path"), *(doc.get("relevant_paths") or [])]:
                root = _norm_path(raw)
                if root and (root, key) not in index._entries:
                    index._entries.append((root, key))
        index._sort_entries()
        return index

    def owner(self, candidate: Optional[str]) -> Optional[str]:
        c = _norm_path(candidate)
        if not c:
            return None
        for root, pid in self._entries:
            if c == root or c.startswith(root + "/"):
                return pid
        return None

    def session_owner(self, session: dict) -> Optional[str]:
        return self.owner(session.get("workspace")) or self.owner(
            session.get("source_repo")
        )


def attention_score(att: dict) -> int:
    """Rank projects by what needs a human: blocked agents first, then failed
    verification, unacked alerts, stale tasks, live activity."""
    return (
        4 * att.get("blocked_shells", 0)
        + 3 * att.get("gate_failed_sessions", 0)
        + 2 * att.get("unacked_alerts", 0)
        + min(att.get("stale_tasks", 0), 5)
        + att.get("running_sessions", 0)
    )


def _last_gate_failed(session: dict) -> bool:
    runs = session.get("gate_runs") or []
    return bool(runs) and not runs[-1].get("passed", True)


async def _gather_context(db, shell_service: ShellService) -> dict:
    """The shared raw material both endpoints aggregate from."""
    now = datetime.now(timezone.utc)
    try:
        shells = await shell_service.fleet_overview()
    except Exception as exc:  # shells substrate down ≠ cockpit down
        logger.debug("cockpit fleet_overview failed: %s", exc)
        shells = []
    sessions = await db.coding_sessions.find(
        {
            "$or": [
                {"status": {"$in": ["starting", "running", "queued"]}},
                {"updated_at": {"$gte": now - timedelta(days=7)}},
            ]
        }
    ).sort("updated_at", -1).to_list(length=300)
    # Alerts that are ASKING for something, which is not the same set as
    # "unacked". `severity: info` is the record-keeping lane: since the
    # notification service stopped dropping `coding:*` events, every coding
    # session writes several info rows (stopped/completed/stall/budget/loop)
    # that no consumer ever acks and no relay ever delivers. Counting those made
    # `2 * unacked_alerts` a permanent session counter, and 300 of them would
    # push the real alerts out of this read entirely. `$ne` rather than an
    # explicit severity list so pre-v2 rows — which have no severity field —
    # keep counting; the alerts that predate the field are exactly the ones that
    # used to reach Ben.
    alerts = await db.alerts.find(
        {"acked": False, "severity": {"$ne": "info"}}
    ).sort("created_at", -1).to_list(length=300)
    return {"shells": shells, "sessions": sessions, "alerts": alerts, "now": now}


def _project_attention(
    project: Project, ctx: dict, open_tasks: list, now: datetime, index: PathIndex
) -> dict:
    shells = [s for s in ctx["shells"] if index.owner(s.get("project_dir")) == project.id]
    sessions = [s for s in ctx["sessions"] if index.session_owner(s) == project.id]
    stale_cutoff = now - timedelta(days=_STALE_TASK_DAYS)

    def _stale(t) -> bool:
        ts = t.updated_at or t.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts < stale_cutoff

    return {
        "shells": len(shells),
        "blocked_shells": sum(
            1 for s in shells if s.get("activity_state") == "blocked" or s.get("awaiting_input")
        ),
        "working_shells": sum(1 for s in shells if s.get("activity_state") == "working"),
        "running_sessions": sum(
            1 for s in sessions if s.get("status") in ("starting", "running", "queued")
        ),
        "gate_failed_sessions": sum(1 for s in sessions if _last_gate_failed(s)),
        # Unacked *and* above info — _gather_context already applied that; the
        # score weights this at 2 per alert, so an info row that never gets
        # acked would add 2 forever.
        "unacked_alerts": sum(
            1
            for a in ctx["alerts"]
            if index.owner(a.get("project_path")) == project.id
        ),
        "open_tasks": len(open_tasks),
        "stale_tasks": sum(1 for t in open_tasks if _stale(t)),
    }


# ------------------------------------------------------------------- routes

def _serialize_cockpit_session(s: dict) -> dict:
    return {
        "id": str(s.get("_id")),
        "backend": s.get("backend"),
        "model": s.get("model"),
        "status": s.get("status"),
        "host": s.get("host"),
        "shell_name": s.get("shell_name"),
        "workspace": s.get("workspace"),
        "looping": bool(s.get("loop_config")),
        "result_summary": s.get("result_summary"),
        "gate_runs": (s.get("gate_runs") or [])[-3:],
        "created_at": s.get("created_at"),
        "updated_at": s.get("updated_at"),
    }
async def _live_git_status(path: Optional[str]) -> Optional[dict]:
    """Branch + dirty-file count from a live `git status` (read-only, fast,
    timeout-bounded). None when the path isn't a local git repo."""
    if not path:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, "status", "--porcelain=v1", "-b",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return None
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        branch = lines[0].removeprefix("## ").split("...")[0] if lines else None
        return {"branch": branch, "dirty_files": max(len(lines) - 1, 0)}
    except Exception:
        return None


@router.get("/projects/overview")
async def projects_overview(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    planning: Annotated[PlanningService, Depends(get_planning_service)],
    shell_service: Annotated[ShellService, Depends(get_shell_service)],
    include_archived: bool = False,
):
    """C4 Project Switcher: every project ranked by what needs attention."""
    projects = await planning.list_projects()
    if not include_archived:
        projects = [p for p in projects if p.status != "archived"]
    ctx = await _gather_context(db, shell_service)
    all_open = await planning.list_tasks(status=["proposed", "active"], limit=1000)
    tasks_by_project: dict[str, list] = {}
    for t in all_open:
        if t.project_id:
            tasks_by_project.setdefault(t.project_id, []).append(t)

    index = PathIndex(projects)
    rows = []
    for p in projects:
        att = _project_attention(p, ctx, tasks_by_project.get(p.id, []), ctx["now"], index)
        rows.append(
            {
                "id": p.id,
                "name": p.name,
                "slug": p.slug,
                "summary": p.summary,
                "status": p.status,
                # `kind` distinguishes a real project from the scratch dirs,
                # worktrees and /tmp paths the harvester also registers. Without
                # it a client cannot separate the two, so the switcher rendered
                # all 64 rows as equal cards — 63 of them noise — and the
                # attention ranking read as broken rather than as unfiltered.
                "kind": getattr(p, "kind", None),
                "charter_purpose": (p.charter or {}).get("purpose") if isinstance(getattr(p, "charter", None), dict) else getattr(getattr(p, "charter", None), "purpose", None),
                "activity_status": p.activity_status,
                "last_activity_at": p.last_activity_at,
                "path": p.path,
                "git": p.git,
                "next_steps": p.next_steps[:3],
                "attention": att,
                "attention_score": attention_score(att),
            }
        )
    rows.sort(
        key=lambda r: (
            -r["attention_score"],
            -(r["last_activity_at"].timestamp() if r["last_activity_at"] else 0),
        )
    )
    return {
        "projects": rows,
        # Key kept (the TUI and web cockpit read it): unacked alerts that need
        # attention, i.e. excluding the info lifecycle lane.
        "unacked_alerts_total": len(ctx["alerts"]),
        "generated_at": ctx["now"],
    }


@router.get("/projects/{ident}/cockpit")
async def project_cockpit(
    ident: str,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    planning: Annotated[PlanningService, Depends(get_planning_service)],
    shell_service: Annotated[ShellService, Depends(get_shell_service)],
):
    """C4 Per-Project Cockpit: everything about one project, in one call."""
    project = await planning.get_project_by_slug(ident) or await planning.get_project(
        ident
    )
    if not project:
        raise HTTPException(status_code=404, detail=f"No project '{ident}'")

    roots = project_roots(project)
    ctx = await _gather_context(db, shell_service)
    now = ctx["now"]

    # Ownership must consider every project's roots, or a path claimed by a
    # more specific sibling/child project would also show up here.
    all_projects = await planning.list_projects()
    if project.id not in {p.id for p in all_projects}:
        all_projects.append(project)
    index = PathIndex(all_projects)

    shells = [
        s for s in ctx["shells"] if index.owner(s.get("project_dir")) == project.id
    ]
    shells.sort(
        key=lambda s: (
            0 if (s.get("activity_state") == "blocked" or s.get("awaiting_input")) else 1,
        )
    )
    sessions = [
        s for s in ctx["sessions"] if index.session_owner(s) == project.id
    ][:25]
    open_tasks = await planning.list_tasks(
        status=["proposed", "active"], project_id=project.id, limit=200
    )
    alerts = [
        a for a in ctx["alerts"] if index.owner(a.get("project_path")) == project.id
    ]
    memories = []
    if roots:
        cursor = db.memories.find(
            {"source.type": "machine_scan", "source.repo": {"$in": roots}}
        ).sort("created_at", -1)
        memories = await cursor.to_list(length=10)

    # Linear read cache (C3): mirrored tickets + proposed dispositions.
    linear_tasks = await db.tasks.find(
        {
            "source.type": "import",
            "external_ref.tracker": "linear",
            "project_id": project.id,
        }
    ).sort("updated_at", -1).to_list(length=100)

    # Budget: priced spend attributed via each session's conversation.
    usage = UsageRepo(db)
    budget = {"cost": 0.0, "total_tokens": 0, "sessions_priced": 0}
    # One aggregation for every session's conversation, not one per session:
    # this loop used to issue up to 25 sequential aggregations.
    conv_ids = [
        c for c in (
            (s.get("agent_conversation_id") or s.get("conversation_id")) for s in sessions
        ) if c
    ]
    try:
        priced = await usage.cost_for_conversations(conv_ids)
    except Exception:
        # The pricing query failed -- report nothing priced rather than
        # claiming sessions were priced at zero (matches the old per-session
        # behaviour, where a failed call skipped the session entirely).
        priced = None
    if priced is not None:
        for conv in conv_ids:
            # sessions_priced counts sessions that HAVE a conversation to
            # price, including ones with no usage rows -- unchanged from the
            # per-session version, where a zero-cost conversation still counted.
            budget["sessions_priced"] += 1
            row = priced.get(conv)
            if not row:
                continue
            budget["cost"] = round(budget["cost"] + row.get("cost", 0.0), 6)
            budget["total_tokens"] += row.get("total_tokens", 0)

    att = _project_attention(project, ctx, open_tasks, now, index)
    stale_cutoff = now - timedelta(days=_STALE_TASK_DAYS)

    def _task_row(t) -> dict:
        ts = t.updated_at or t.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return {**t.model_dump(), "stale": ts < stale_cutoff}

    vault_folder = None
    if project.path and settings.obsidian_vault_path:
        import os
        candidate = os.path.join(
            settings.obsidian_vault_path, os.path.basename(project.path.rstrip("/"))
        )
        if os.path.isdir(candidate):
            vault_folder = candidate

    return {
        "project": project.model_dump(),
        "attention": att,
        "attention_score": attention_score(att),
        "git": {
            "harvested": project.git,
            "live": await _live_git_status(project.path),
        },
        "shells": shells,
        "sessions": [_serialize_cockpit_session(s) for s in sessions],
        "tasks": [_task_row(t) for t in open_tasks],
        "alerts": [
            {
                "id": str(a.get("_id")),
                "source": a.get("source"),
                "event_type": a.get("event_type"),
                "message": a.get("message"),
                "created_at": a.get("created_at"),
            }
            for a in alerts
        ],
        "changed": [
            {"content": m.get("content"), "created_at": m.get("created_at")}
            for m in memories
        ],
        "linear": [
            {
                "id": t.get("id") or str(t.get("_id")),
                "title": t.get("title"),
                "status": t.get("status"),
                "external_ref": t.get("external_ref"),
                "proposed_disposition": t.get("proposed_disposition"),
                "updated_at": t.get("updated_at"),
            }
            for t in linear_tasks
        ],
        "budget": budget,
        "vault_folder": vault_folder,
        "generated_at": now,
    }
