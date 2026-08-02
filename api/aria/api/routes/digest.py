"""
ARIA - Project Switcher & Per-Project Cockpit read models (Coherence C4)

Purpose: the grounding surface. `/projects/overview` answers "where is
everything, and what needs me?" (all projects ranked by attention);
`/projects/{ident}/cockpit` is the focused per-project aggregate (git, agents,
tasks, verification, what-changed memories, alerts, budget). Pure read models
over existing collections — no new storage beyond the fixed-_id active-project
doc.

Related Spec Sections:
- COHERENCE_DESIGN.md C4 (Project Cockpit)
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
_ACTIVE_PROJECT_DOC = "global"


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


def _session_matches(session: dict, roots: list[str]) -> bool:
    return path_in_project(session.get("workspace"), roots) or path_in_project(
        session.get("source_repo"), roots
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
    alerts = await db.alerts.find({"acked": False}).sort("created_at", -1).to_list(
        length=300
    )
    return {"shells": shells, "sessions": sessions, "alerts": alerts, "now": now}


def _project_attention(
    project: Project, ctx: dict, open_tasks: list, now: datetime
) -> dict:
    roots = project_roots(project)
    shells = [s for s in ctx["shells"] if path_in_project(s.get("project_dir"), roots)]
    sessions = [s for s in ctx["sessions"] if _session_matches(s, roots)]
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
        "unacked_alerts": sum(
            1
            for a in ctx["alerts"]
            if path_in_project(a.get("project_path"), roots)
        ),
        "open_tasks": len(open_tasks),
        "stale_tasks": sum(1 for t in open_tasks if _stale(t)),
    }


async def _get_active_project_slug(db) -> Optional[str]:
    doc = await db.app_state.find_one({"_id": _ACTIVE_PROJECT_DOC})
    return (doc or {}).get("active_project_slug")


# ------------------------------------------------------------------- routes

class ActiveProjectRequest(BaseModel):
    slug: Optional[str] = Field(
        default=None, description="Project slug to focus, or null to clear"
    )


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

    rows = []
    for p in projects:
        att = _project_attention(p, ctx, tasks_by_project.get(p.id, []), ctx["now"])
        rows.append(
            {
                "id": p.id,
                "name": p.name,
                "slug": p.slug,
                "summary": p.summary,
                "status": p.status,
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
        "active_project": await _get_active_project_slug(db),
        "unacked_alerts_total": len(ctx["alerts"]),
        "generated_at": ctx["now"],
    }


@router.get("/projects/active")
async def get_active_project(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    return {"active_project": await _get_active_project_slug(db)}


@router.put("/projects/active")
async def set_active_project(
    request: ActiveProjectRequest,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    planning: Annotated[PlanningService, Depends(get_planning_service)],
):
    """Persist the server-side focus (shared by web/TUI/CLI/Hermes)."""
    if request.slug is not None:
        project = await planning.get_project_by_slug(request.slug)
        if not project:
            raise HTTPException(status_code=404, detail=f"No project '{request.slug}'")
    await db.app_state.update_one(
        {"_id": _ACTIVE_PROJECT_DOC},
        {
            "$set": {
                "active_project_slug": request.slug,
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    return {"active_project": request.slug}


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

    shells = [
        s for s in ctx["shells"] if path_in_project(s.get("project_dir"), roots)
    ]
    shells.sort(
        key=lambda s: (
            0 if (s.get("activity_state") == "blocked" or s.get("awaiting_input")) else 1,
        )
    )
    sessions = [s for s in ctx["sessions"] if _session_matches(s, roots)][:25]
    open_tasks = await planning.list_tasks(
        status=["proposed", "active"], project_id=project.id, limit=200
    )
    alerts = [
        a for a in ctx["alerts"] if path_in_project(a.get("project_path"), roots)
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
    for s in sessions:
        conv = s.get("agent_conversation_id") or s.get("conversation_id")
        if not conv:
            continue
        try:
            row = await usage.cost_for_conversation(conv)
        except Exception:
            continue
        budget["cost"] = round(budget["cost"] + row.get("cost", 0.0), 6)
        budget["total_tokens"] += row.get("total_tokens", 0)
        budget["sessions_priced"] += 1

    att = _project_attention(project, ctx, open_tasks, now)
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
