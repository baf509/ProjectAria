#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=1.2,<2",  # 2.0.0 removed mcp.server.fastmcp (2026-08-15: gateway parked the aria MCP 251x/24h)
#   "httpx>=0.27",
# ]
# ///
"""MCP server wrapping ProjectAria's /api/v1 endpoints.

Exposed to agents (e.g. the Hermes/Nous agent) as the `aria` MCP server. This is
the absorbed successor to aria-shells' MCP bridge: ProjectAria is now the single
always-on service, so the projects/tasks tools target ProjectAria's native
planning routes (/todos + /projects/{id|slug}) rather than aria-shells' old
/tasks + /projects/{slug} shapes. The shell tools are unchanged.

Tool groups:
  - Fleet status   : fleet_status, list_shells, get_shell, aria_health
  - Reading a shell: get_shell_screen, get_shell_snapshot, get_shell_events, search_shells
  - Driving a shell: send_shell_input, create_shell, delete_shell, set_shell_tags, resize_shell
  - Projects/tasks : list_projects, get_project, list_tasks, create_task, update_task
  - Alerts (relay) : list_alerts, ack_alert, decide_alert, mark_alert_delivered,
                     relay_heartbeat — ProjectAria queues alerts here and Hermes
                     relays them over Signal, since ProjectAria no longer pushes
                     notifications directly. Only `needs_human` alerts are for
                     Ben; the rest are cockpit material.
  - Model servers  : list_model_servers, list_gpu_devices,
                     model_server_utilization, start_model_server,
                     stop_model_server, bind_model_server, unbind_model_server
                     — the local LLM control plane (see
                     aria.infrastructure.model_servers). start_model_server
                     also chooses HOW a model loads (device placement, context,
                     KV type, drafter) via `overrides`.

ProjectAria listens on :8200 after the cutover (it inherited aria-shells' port).
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

ARIA_BASE = os.environ.get("ARIA_API_URL", "http://127.0.0.1:8200").rstrip("/")
ARIA_KEY = os.environ.get("ARIA_API_KEY", "")
TIMEOUT = float(os.environ.get("ARIA_HTTP_TIMEOUT", "20"))

mcp = FastMCP("aria")


def _client() -> httpx.AsyncClient:
    headers = {"Accept": "application/json"}
    if ARIA_KEY:
        headers["X-API-Key"] = ARIA_KEY
    return httpx.AsyncClient(base_url=ARIA_BASE, headers=headers, timeout=TIMEOUT)


async def _request(method: str, path: str, **kw: Any) -> Any:
    async with _client() as c:
        r = await c.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"ARIA {method} {path} -> {r.status_code}: {r.text[:500]}")
        if not r.content:
            return None
        ctype = r.headers.get("content-type", "")
        return r.json() if "application/json" in ctype else r.text


def _one_id(canonical: Optional[str], alias: Optional[str], name: str) -> str:
    """Resolve a canonical `<thing>_id` parameter against a bare `id` alias.

    Every listing endpoint serializes its primary key as plain `id` (or `_id`
    for workflows), so a model that reads a list and then calls the matching
    action naturally passes `id=...`. Rejecting that is a pure contract wart —
    it cost a gemma-backed alert-triage cron an infinite retry loop on
    2026-07-30. Accept either spelling; require exactly one.
    """
    value = canonical or alias
    if not value:
        raise ValueError(f"{name} is required (pass `{name}` or its `id` alias)")
    return value


async def _resolve_project(slug_or_id: str) -> dict:
    """Fetch a project by slug or id. ProjectAria's /projects/{id} accepts both."""
    return await _request("GET", f"/api/v1/projects/{slug_or_id}")


# Map the agent-facing task status vocabulary onto ProjectAria's lifecycle.
# ProjectAria todos use proposed|active|done|dismissed; "open" == not-yet-closed.
_OPEN_STATUSES = "proposed,active"


def _map_task_status(status: Optional[str]) -> Optional[str]:
    if not status:
        return None
    if status == "open":
        return _OPEN_STATUSES
    return status


# ───────────────────────────────────────────────────────── fleet status ──

@mcp.tool()
async def fleet_status(awaiting_only: bool = False) -> dict:
    """Digest of the whole watched-shell fleet in ONE call — start here.

    Per shell: status, idle_seconds, awaiting_input (blocked on a human),
    prompt_line, last_line. Shells awaiting input sort first; `awaiting_count`
    says how many. Prefer over list_shells + per-shell snapshots for "what is my
    fleet doing?" / "is anything waiting on me?". awaiting_only=True filters to
    blocked shells.
    """
    params = {"awaiting": "true"} if awaiting_only else None
    overview = await _request("GET", "/api/v1/shells/overview", params=params)
    # Enrich with the coding sub-agent concurrency gauge (active/queued/limit)
    # and the rolling cache-hit rate. Best-effort — a failure never breaks the
    # fleet digest.
    if isinstance(overview, dict):
        try:
            overview["coding_concurrency"] = await _request(
                "GET", "/api/v1/coding/sessions/concurrency"
            )
        except Exception:
            pass
        try:
            summary = await _request("GET", "/api/v1/usage/summary", params={"days": 1})
            if isinstance(summary, dict):
                overview["cache_hit_rate"] = summary.get("cache_hit_rate", 0.0)
        except Exception:
            pass
    return overview


@mcp.tool()
async def list_shells(status: Optional[str] = None) -> dict:
    """List watched shells (metadata only). status: 'active', 'idle', 'stopped',
    or comma-separated. For an activity/attention digest use fleet_status."""
    params = {"status": status} if status else None
    return await _request("GET", "/api/v1/shells", params=params)


@mcp.tool()
async def get_shell(name: str) -> dict:
    """Get metadata for one shell (full or short name)."""
    return await _request("GET", f"/api/v1/shells/{name}")


@mcp.tool()
async def aria_health() -> dict:
    """Quick health of the ProjectAria stack: database, embeddings, and LLM
    backend AVAILABILITY (config/SDK presence, not real reachability — a
    backend can report available here and still be down). Use this for a
    fast up/down check; use health_services for real per-backend reachability."""
    return await _request("GET", "/api/v1/health")


@mcp.tool()
async def health_services() -> dict:
    """Real reachability probe of every backing service (mongod, mongot, the
    local LLM servers, embeddings, tts, stt) — actual HTTP pings with latency,
    not just config/SDK presence like aria_health. A 401/403 counts as
    unhealthy (a rejected credential is a real failure). Ridge is deliberately
    NOT probed here since it sleeps by design; a probe would either report it
    falsely down or wake it every check. Returns {services: [...], healthy, total}."""
    return await _request("GET", "/api/v1/health/services")


# ────────────────────────────────────────────────────────── reading ──

@mcp.tool()
async def get_shell_screen(name: str, lines: int = 40) -> dict:
    """Capture a shell's visible pane RIGHT NOW (fresh, ANSI-stripped).

    Best for "what's on screen at this moment", e.g. after sending input.
    For the last worker-stored snapshot (can be ~30s old) use
    get_shell_snapshot; for raw line history use get_shell_events."""
    return await _request("GET", f"/api/v1/shells/{name}/screen", params={"lines": lines})


@mcp.tool()
async def get_shell_snapshot(name: str) -> dict:
    """Return the latest worker-stored visible-pane snapshot of a shell
    (refreshed every ~30s). For a live capture use get_shell_screen."""
    return await _request("GET", f"/api/v1/shells/{name}/snapshot")


@mcp.tool()
async def get_shell_events(
    name: str,
    since_line: Optional[int] = None,
    limit: int = 200,
    kinds: Optional[str] = None,
) -> dict:
    """Fetch recent event lines from a shell (raw captured output/input).
    kinds: comma-separated subset of 'output,input' (default both).
    Pass since_line to page forward from a previous call."""
    params: dict[str, Any] = {"limit": limit}
    if since_line is not None:
        params["since_line"] = since_line
    if kinds:
        params["kinds"] = kinds
    return await _request("GET", f"/api/v1/shells/{name}/events", params=params)


@mcp.tool()
async def search_shells(q: str, limit: int = 25) -> dict:
    """Search the fleet for `q`. Returns two lists:
      - `shells`: shells whose name / project_dir / tags match.
      - `events`: matching lines of captured output (full-text)."""
    return await _request("GET", "/api/v1/shells/search", params={"q": q, "limit": limit})


# ────────────────────────────────────────────────────────── driving ──

@mcp.tool()
async def send_shell_input(
    name: str,
    text: str,
    append_enter: bool = True,
    literal: bool = False,
    wait_ms: int = 0,
) -> dict:
    """Type text into a shell. append_enter sends a newline (submission) after.
    literal=True passes -l to tmux so key names aren't expanded.

    Set wait_ms (e.g. 1500) to have the server wait that long after sending and
    return the resulting screen in the `screen` field — a single call to act and
    observe the effect, instead of send-then-poll. Capped at 10000ms."""
    body = {
        "text": text,
        "append_enter": append_enter,
        "literal": literal,
        "wait_ms": wait_ms,
    }
    return await _request("POST", f"/api/v1/shells/{name}/input", json=body)


@mcp.tool()
async def nudge_paused_shell(
    name: str, text: Optional[str] = None, force: bool = False
) -> dict:
    """Nudge a watched shell paused at a prompt (activity_state 'blocked' in
    fleet_status). Sends Enter at a safe 'press enter' prompt, else a continue
    instruction; after 3 consecutive failed nudges it alerts the human. Safe on
    every sweep — not-paused, recently-nudged, or just-paused shells are skipped
    (see `reason`). `text` overrides the message; force=true skips the guards."""
    body: dict[str, Any] = {"force": force}
    if text is not None:
        body["text"] = text
    return await _request("POST", f"/api/v1/shells/{name}/nudge", json=body)


@mcp.tool()
async def create_shell(
    name: str,
    workdir: Optional[str] = None,
    launch_claude: bool = True,
    cols: Optional[int] = None,
    rows: Optional[int] = None,
) -> dict:
    """Create a new watched tmux shell.
    launch_claude=True spawns the configured claude command inside it."""
    body: dict[str, Any] = {"name": name, "launch_claude": launch_claude}
    if workdir:
        body["workdir"] = workdir
    if cols:
        body["cols"] = cols
    if rows:
        body["rows"] = rows
    return await _request("POST", "/api/v1/shells", json=body)


@mcp.tool()
async def delete_shell(name: str, purge: bool = False) -> dict:
    """Stop tracking a shell (kills the tmux session). purge=True also deletes
    its stored events and snapshots."""
    params = {"purge": "true"} if purge else None
    return await _request("DELETE", f"/api/v1/shells/{name}", params=params)


@mcp.tool()
async def set_shell_tags(name: str, tags: list[str]) -> dict:
    """Replace the tag list on a shell."""
    return await _request("POST", f"/api/v1/shells/{name}/tags", json={"tags": tags})


@mcp.tool()
async def resize_shell(name: str, cols: int, rows: int) -> dict:
    """Resize the tmux pane of a shell (so a TUI repaints at your viewport)."""
    return await _request("POST", f"/api/v1/shells/{name}/resize", json={"cols": cols, "rows": rows})


# ──────────────────────────────────────────────────── projects / tasks ──

@mcp.tool()
async def list_projects(status: Optional[str] = None) -> dict:
    """List projects (harvested from git repos + Claude/pi sessions + live
    shells, merged with conversationally-tracked ones). status filters by
    machine activity_status: 'active' or 'idle'."""
    data = await _request("GET", "/api/v1/projects")
    projects = data.get("projects", []) if isinstance(data, dict) else []
    if status:
        projects = [p for p in projects if p.get("activity_status") == status]
    return {"projects": projects}


@mcp.tool()
async def get_project(slug: str) -> dict:
    """Get one project by slug (or id), including its open tasks and derived
    git/source info."""
    proj = await _resolve_project(slug)
    pid = proj.get("id")
    tasks: list = []
    if pid:
        td = await _request("GET", f"/api/v1/projects/{pid}/tasks")
        tasks = td.get("tasks", []) if isinstance(td, dict) else []
    return {"project": proj, "tasks": tasks}


@mcp.tool()
async def projects_overview(include_archived: bool = False) -> dict:
    """Coherence C4 Project Switcher: every project ranked by what needs a
    human — blocked agents, failed verification gates, unacked alerts, stale
    tasks. The one-call answer to "where is everything, and what needs me?".
    Also returns the persisted active (focused) project."""
    return await _request(
        "GET", "/api/v1/projects/overview",
        params={"include_archived": include_archived},
    )


@mcp.tool()
async def project_cockpit(slug: str) -> dict:
    """Coherence C4 Per-Project Cockpit: the full focused picture for one
    project (by slug or id) — live git status, its agents/shells (blocked
    first), coding sessions with verification gate_runs, open+stale tasks,
    what-changed memories, scoped alerts, Linear tickets, and priced spend."""
    return await _request("GET", f"/api/v1/projects/{slug}/cockpit")


@mcp.tool()
async def create_linear_ticket(
    title: str, description: str = "", project: Optional[str] = None
) -> dict:
    """Create a Linear ticket (the Signal → Hermes → Linear capture path).
    project is an ARIA project slug from the configured linear_project_map;
    omit it when only one project is mapped. Returns the created issue's
    identifier and url. Fails with 409 when the Linear integration is
    disabled."""
    body: dict[str, Any] = {"title": title, "description": description}
    if project is not None:
        body["project"] = project
    return await _request("POST", "/api/v1/linear/tickets", json=body)


@mcp.tool()
async def publish_to_obsidian(
    content: str,
    title: str,
    doc_type: str = "Analysis",
    project: Optional[str] = None,
) -> dict:
    """Publish long-form markdown (an analysis, design draft, research
    report) into Ben's Obsidian vault, where it syncs to all his devices.
    doc_type picks the subfolder: Design, Specs, Analysis, Research, or
    Planning. project is a repo path or vault folder name (e.g.
    '/home/ben/Development/ProjectAria' or 'ProjectAria'); omit it for
    ARIA's general folder. Writes are atomic and never overwrite a doc a
    human recently edited. Returns the vault path written."""
    body: dict[str, Any] = {"content": content, "title": title, "doc_type": doc_type}
    if project is not None:
        body["project"] = project
    return await _request("POST", "/api/v1/obsidian/publish", json=body)


@mcp.tool()
async def retire_project(project: str, dry_run: bool = True) -> dict:
    """Retire a project: distil its transcripts into long-term memory, then
    remove it from the board.

    Memories are written and verified BEFORE anything is deleted, so a failed
    extraction leaves the project intact. Scrollback, coding sessions and
    previously-extracted memories are kept — only the project row and its tasks
    go. Refuses while the project still has a running session or an active
    shell.

    Defaults to dry_run=True: call once to see what would move and what would
    go, then again with dry_run=False.
    """
    proj = await _resolve_project(project)
    slug = proj.get("slug") if isinstance(proj, dict) else project
    return await _request("POST", f"/api/v1/projects/{slug}/retire", json={"dry_run": dry_run})


@mcp.tool()
async def list_tasks(project: Optional[str] = None, status: Optional[str] = None) -> dict:
    """List to-do tasks, optionally filtered by project (slug or id) and/or
    status ('open', 'proposed', 'active', 'done', 'dismissed')."""
    params: dict[str, Any] = {}
    mapped = _map_task_status(status)
    if mapped:
        params["status"] = mapped
    if project:
        proj = await _resolve_project(project)
        params["project_id"] = proj.get("id")
    return await _request("GET", "/api/v1/todos", params=params or None)


@mcp.tool()
async def create_task(
    title: str,
    project_slug: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Create a to-do task, optionally attached to a project (slug or id)."""
    body: dict[str, Any] = {"title": title}
    if notes:
        body["notes"] = notes
    if project_slug:
        proj = await _resolve_project(project_slug)
        body["project_id"] = proj.get("id")
    return await _request("POST", "/api/v1/todos", json=body)


@mcp.tool()
async def update_task(
    task_id: str,
    status: Optional[str] = None,
    title: Optional[str] = None,
    notes: Optional[str] = None,
    project_slug: Optional[str] = None,
) -> dict:
    """Update a to-do task by id. Set status='done' to complete it (stamps
    completed_at). Only provided fields are changed."""
    # 'done' has a dedicated endpoint that stamps completed_at consistently.
    if status == "done" and not (title or notes or project_slug):
        return await _request("POST", f"/api/v1/todos/{task_id}/done")
    body: dict[str, Any] = {}
    if status is not None:
        body["status"] = status
    if title is not None:
        body["title"] = title
    if notes is not None:
        body["notes"] = notes
    if project_slug is not None:
        proj = await _resolve_project(project_slug)
        body["project_id"] = proj.get("id")
    if not body:
        raise RuntimeError("update_task: provide at least one field to change")
    return await _request("PATCH", f"/api/v1/todos/{task_id}", json=body)


# ──────────────────────────────────────────────────────────── alerts ──
# ProjectAria no longer pushes notifications itself; it queues alerts here and
# Hermes (which owns the signal-cli daemon) relays them over Signal, then acks.

@mcp.tool()
async def list_alerts(
    unacked_only: bool = True,
    limit: int = 50,
    needs_human_only: bool = False,
    undelivered_only: bool = False,
    severity: Optional[str] = None,
    kind: Optional[str] = None,
    project: Optional[str] = None,
) -> dict:
    """List ProjectAria alerts.

    ⚠️ Relaying to Ben wants `needs_human_only=true, undelivered_only=true`.
    That is the small set a human must actually decide. Everything else —
    session stalls, budget notices, recoveries — is `severity="info"` cockpit
    and digest material and must NOT be sent to Signal: relaying lifecycle
    noise is what trains a person to stop reading their own alert queue.
    Deliver → mark_alert_delivered → ack_alert."""
    params: dict[str, Any] = {"limit": limit}
    if unacked_only:
        params["unacked_only"] = "true"
    if needs_human_only:
        params["needs_human"] = "true"
    if undelivered_only:
        params["undelivered"] = "true"
    if severity:
        params["severity"] = severity
    if kind:
        params["kind"] = kind
    if project:
        params["project"] = project
    return await _request("GET", "/api/v1/alerts", params=params)


@mcp.tool()
async def decide_alert(
    action: str,
    alert_id: Optional[str] = None,
    id: Optional[str] = None,
    by: str = "ben",
    note: Optional[str] = None,
) -> dict:
    """Record Ben's typed answer to a raise: APPLY | REJECT | STOP | HOLD | IGNORE.

    The reply grammar is typed on purpose: the previous flow relied on recalling
    from conversation memory which fix a bare "APPLY" referred to, so nothing
    bound the decision to an alert and there was no audit trail. IGNORE means
    the raise was unnecessary and feeds the false-raise metric. Acks the alert."""
    return await _request(
        "POST",
        f"/api/v1/alerts/{_one_id(alert_id, id, 'alert_id')}/decide",
        json={"action": action, "by": by, "note": note},
    )


@mcp.tool()
async def mark_alert_delivered(
    alert_id: Optional[str] = None, id: Optional[str] = None, by: str = "hermes-outbox"
) -> dict:
    """Call after a Signal send succeeds. Delivered is NOT acked: delivered means
    Ben saw it, acked means it is closed. Keeping them separate is what makes a
    dead relay visible instead of looking like a quiet week."""
    return await _request(
        "POST",
        f"/api/v1/alerts/{_one_id(alert_id, id, 'alert_id')}/delivered",
        json={"by": by},
    )


@mcp.tool()
async def list_active_projects() -> dict:
    """The ACTIVE SET the steward acts on: status=active AND kind=project AND an
    approved charter with a purpose. Everything else in the registry is
    inventory — 59 rows were being tracked as "projects" including Downloads,
    /tmp/workspace and .worktrees/*, which is why the attention ranking read
    zero for everything. Use list_projects for the full inventory."""
    return await _request("GET", "/api/v1/projects/active-set")


@mcp.tool()
async def get_project_charter(slug: str) -> dict:
    """A project's charter — purpose, goals, non-goals, research topics,
    autonomy, allowed tiers, cadence, budget, guard — plus ARIA's steward state
    and the budget with config defaults already resolved."""
    return await _request("GET", f"/api/v1/projects/{slug}/charter")


@mcp.tool()
async def set_project_charter(
    slug: str,
    purpose: Optional[str] = None,
    goals: Optional[list] = None,
    success_criteria: Optional[list] = None,
    non_goals: Optional[list] = None,
    research_topics: Optional[list] = None,
    autonomy: Optional[int] = None,
    tiers_allowed: Optional[list] = None,
    cadence: Optional[dict] = None,
    budget: Optional[dict] = None,
    guard: Optional[dict] = None,
) -> dict:
    """Set or amend a project's charter. PARTIAL merge: only what you pass is
    written, so amending a budget cannot blank the purpose.

    Autonomy: 0 observe / 1 propose / 2 execute in a sandboxed worktree with a
    merge gate (local models cap here) / 3 auto-merge behind the full gate.

    ⚠️ A charter is Ben's statement of what a project is FOR — it drives what
    the steward researches and what agents are allowed to do unattended. Relay
    his words; never invent one to fill the field."""
    charter = {
        k: v for k, v in {
            "purpose": purpose, "goals": goals, "success_criteria": success_criteria,
            "non_goals": non_goals, "research_topics": research_topics,
            "autonomy": autonomy, "tiers_allowed": tiers_allowed,
            "cadence": cadence, "budget": budget, "guard": guard,
        }.items() if v is not None
    }
    return await _request(
        "PUT", f"/api/v1/projects/{slug}/charter",
        json={"charter": charter, "via": "mcp"},
    )


@mcp.tool()
async def steward_status() -> dict:
    """What the steward is doing: whether it is enabled, its last tick per
    chartered project, what it chose and why, and which projects are in the
    active set (status=active AND kind=project AND a charter with a purpose)."""
    return await _request("GET", "/api/v1/steward/status")


@mcp.tool()
async def steward_runs(limit: int = 20, project: str = "") -> dict:
    """Recent steward ticks — what it saw, what it chose, and the reason. This is
    the audit trail for autonomous action; read it before answering "why did it
    do that?"."""
    params: dict[str, Any] = {"limit": limit}
    if project:
        params["project"] = project
    return await _request("GET", "/api/v1/steward/runs", params=params)


@mcp.tool()
async def steward_tick(slug: str) -> dict:
    """Run one steward tick for a project NOW instead of waiting for the timer.

    Respects that project's charter autonomy and budget exactly as the scheduled
    tick would — this is a "do it now", not a "do it anyway"."""
    return await _request("POST", f"/api/v1/steward/projects/{slug}/tick")


@mcp.tool()
async def improve_status() -> dict:
    """The self-improvement loop: whether it is enabled, the current baseline
    metrics, and how many clean promotions have accumulated. Improvement is
    gated on measured outcomes — with no baseline it proposes nothing."""
    return await _request("GET", "/api/v1/improve/status")


@mcp.tool()
async def improve_proposals(limit: int = 20, status: str = "") -> dict:
    """Proposed changes to ARIA's own prompts/thresholds, with their gate
    evidence. Promotion needs the admin key, which MCP deliberately does not
    have — relay the proposal to Ben, do not try to apply it."""
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    return await _request("GET", "/api/v1/improve/proposals", params=params)


@mcp.tool()
async def guard_status() -> dict:
    """Guard health: sandbox preflight (bwrap/systemd-run present, MemAvailable,
    whether a spawn is allowed), the enforced policy hash and its tamper
    verdict, and event/checkpoint counts."""
    return await _request("GET", "/api/v1/guard/status")


@mcp.tool()
async def guard_events(limit: int = 50, session_id: str = "", blocked_only: bool = False) -> dict:
    """Recent guard events — blocked actions, protected-path touches, tamper
    checks, checkpoints, merges. blocked_only=True is the raise-worthy subset."""
    params: dict[str, Any] = {"limit": limit, "blocked_only": blocked_only}
    if session_id:
        params["session_id"] = session_id
    return await _request("GET", "/api/v1/guard/events", params=params)


@mcp.tool()
async def guard_checkpoints(session_id: str = "", limit: int = 20) -> dict:
    """Checkpoint commits for a coding session. Each sha is a rollback target;
    ARIA makes these commits itself so an agent cannot skip its own checkpoint."""
    params: dict[str, Any] = {"limit": limit}
    if session_id:
        params["session_id"] = session_id
    return await _request("GET", "/api/v1/guard/checkpoints", params=params)


@mcp.tool()
async def checkpoint_coding_session(session_id: str, reason: str = "manual") -> dict:
    """Commit a coding session's current worktree state. No-op on a clean tree."""
    return await _request(
        "POST", f"/api/v1/guard/sessions/{session_id}/checkpoint", json={"reason": reason}
    )


@mcp.tool()
async def rollback_coding_session(session_id: str, to: str = "start") -> dict:
    """git reset --hard inside that session's worktree ONLY. 'start' is the
    pre-session tag aria/ckpt/<sid>/start. The live checkout is never touched."""
    return await _request(
        "POST", f"/api/v1/guard/sessions/{session_id}/rollback", json={"to": to}
    )


@mcp.tool()
async def coding_session_merge_gate(session_id: str) -> dict:
    """Run the merge gate — check command, diff size, protected paths, gitleaks,
    charter allowed_paths — and return the verdict.

    The check command comes from the project's own `check_command` or the
    configured default; a caller cannot supply one (that parameter was a remote
    shell with the admin key in scope, removed 2026-08-15). It NEVER merges: a
    merge at autonomy <= 2 is Ben's APPLY and needs the admin key, which MCP
    deliberately does not have."""
    return await _request("GET", f"/api/v1/guard/sessions/{session_id}/merge-gate")


@mcp.tool()
async def vault_poll() -> dict:
    """Read Ben's Obsidian vault control docs now (CHARTER.md, STEWARD_PLAN.md,
    Research/*.md) and return what a HUMAN changed: approval/autonomy/accepted
    flips, charter edits, the '## Notes from Ben' section, and parse errors.

    Human edits are detected by content hash against what ARIA last wrote, not
    by mtime — the LiveSync bridge rewrites mtimes, so mtime cannot tell Ben's
    edit from a sync echo of ARIA's own write."""
    return await _request("POST", "/api/v1/vault/poll")


@mcp.tool()
async def vault_events(limit: int = 50) -> dict:
    """Recent vault change events (newest last)."""
    return await _request("GET", "/api/v1/vault/events", params={"limit": limit})


@mcp.tool()
async def relay_heartbeat(source: str = "hermes-outbox") -> dict:
    """Report that the outbox relay ran — EVERY pass, including quiet ones.

    Silence for alert_relay_heartbeat_timeout_minutes makes ARIA raise
    relay:dead, write STEWARD_INBOX.md into the vault, and send one break-glass
    Signal message. A relay that only heartbeats when it has something to say
    cannot be distinguished from a dead one."""
    return await _request("POST", "/api/v1/alerts/relay-heartbeat", json={"source": source})


@mcp.tool()
async def ack_alert(alert_id: Optional[str] = None, id: Optional[str] = None) -> dict:
    """Acknowledge an alert by id so it is not relayed again. Takes the id
    under either `alert_id` or `id` — list_alerts returns it as `id`."""
    return await _request("POST", f"/api/v1/alerts/{_one_id(alert_id, id, 'alert_id')}/ack")


# ─────────────────────────────────────────────────── ARIA chat / orchestrator ──
# Talk to ARIA herself (the orchestrator agent, currently GLM 5.2). Unlike a
# watched shell — a foreign Claude process ARIA only observes — this drives
# ARIA's own brain: her memory, tools, and configured model.

@mcp.tool()
async def chat(
    message: str,
    conversation_id: Optional[str] = None,
    agent_slug: Optional[str] = None,
) -> dict:
    """Send a message to ARIA and get her reply (non-streaming).

    Omit conversation_id to start a new conversation (uses the default ARIA
    orchestrator agent unless you pass agent_slug, e.g. 'pi-coding'). Check
    list_agents first — a disabled agent (enabled=false, e.g. 'search-agent',
    paused 2026-07-28) is refused with a 400, not silently ignored. Returns
    {content, conversation_id, tool_calls, usage} —
    pass the returned conversation_id back to continue the thread."""
    if not conversation_id:
        body: dict[str, Any] = {}
        if agent_slug:
            body["agent_slug"] = agent_slug
        conv = await _request("POST", "/api/v1/conversations", json=body)
        conversation_id = conv.get("id")
    resp = await _request(
        "POST",
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": message, "stream": False},
    )
    out = resp if isinstance(resp, dict) else {"content": str(resp)}
    out["conversation_id"] = conversation_id
    return out


@mcp.tool()
async def list_conversations(status: str = "active", limit: int = 20) -> Any:
    """List ARIA conversations (default: active). For reading one, use
    read_conversation."""
    return await _request(
        "GET", "/api/v1/conversations", params={"status": status, "limit": limit}
    )


@mcp.tool()
async def get_usage_cost(days: int = 7) -> Any:
    """Total $ cost over the last N days, broken down by (model, backend).
    Local backends cost $0; this is mainly for spotting unexpected cloud
    spend (e.g. an unpinned coding session routing to Opus more than
    expected). See /usage/by-session or /usage/by-conversation in the REST
    API directly if you need finer granularity than this exposes."""
    return await _request("GET", "/api/v1/usage/cost", params={"days": days})


@mcp.tool()
async def read_conversation(
    conversation_id: Optional[str] = None, message_limit: int = 20, id: Optional[str] = None,
) -> dict:
    """Read one conversation including its recent messages. Takes the id under
    either `conversation_id` or `id` — list_conversations returns it as `id`."""
    return await _request(
        "GET", f"/api/v1/conversations/{_one_id(conversation_id, id, 'conversation_id')}",
        params={"msg_limit": message_limit},
    )


@mcp.tool()
async def list_agents() -> Any:
    """List ARIA's agent personas (orchestrator + delegated agents) with their
    configured model/backend and tools."""
    return await _request("GET", "/api/v1/agents")


@mcp.tool()
async def update_agent(
    agent_slug: str,
    enabled: Optional[bool] = None,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> dict:
    """Enable/disable an agent or repoint its backend/model. `agent_slug`
    takes a slug ('search-agent', 'pi-coding') or raw id.

    ⚠️ SINCE 2026-08-15 THIS REQUIRES THE ADMIN KEY AND WILL RETURN 403 HERE.
    Repointing an agent changes what every future session does, durably and
    invisibly, so `PUT /agents/{id}` moved behind ADMIN_KEY (steward plan §7.4)
    — and ADMIN_KEY is deliberately not available to MCP, because MCP is
    reachable by Hermes and therefore by anything that can talk to Hermes.
    Ben applies these from the TUI/CLI. Report the 403 and what you wanted to
    change; do not try to route around it by editing the database.

    backend/model use the LLM-ADAPTER vocabulary (llamacpp, agentic, ridge,
    anthropic...) — NOT create_coding_session's (claude_code/codex/pi-code/
    pool)."""
    body: dict[str, Any] = {}
    if enabled is not None:
        body["enabled"] = enabled
    if backend is not None or model is not None or temperature is not None:
        current = await _request("GET", f"/api/v1/agents/{agent_slug}")
        llm = dict(current.get("llm") or {})
        if backend is not None:
            llm["backend"] = backend
        if model is not None:
            llm["model"] = model
        if temperature is not None:
            llm["temperature"] = temperature
        body["llm"] = llm
    return await _request("PUT", f"/api/v1/agents/{agent_slug}", json=body)


# ─────────────────────────────────────────────────────────── model servers ──
# The local LLM model-server control plane (aria.infrastructure.model_servers).
# As of 2026-07-29 ALL model-server start/stop on corsair-ai goes through this
# — not manual docker/docker compose. Each server runs a DIFFERENT llama.cpp
# fork/build (Vulkan vs HIP, different repos); mixing a model with the wrong
# one either refuses to load or can wedge the GPU. start() hard-refuses on a
# RAM-exclusivity conflict or a live-GTT-usage SWAG overflow unless force=True.
# bind()/unbind() pair a server with an agent slug — purely descriptive (it
# does not change the agent's actual llm.backend/model routing), enforced
# one-agent-per-server unless force=True adds an extra slot.

@mcp.tool()
async def list_model_servers() -> Any:
    """Every registered local model+runtime pair (+ the off-box Ridge entry):
    live state, runtime fork, device placement, memory pool, footprint estimate
    and which agent (if any) it is bound to.

    Two fields answer "how do I load this differently":
      - `devices` / `memory_pool` — WHERE it runs. Entries on `r9700-vram` and
        entries on `halo-gtt` can be resident at the same time.
      - `parameters` — HOW it loads. Each knob carries its effective `value`
        and the `source` of that value, and any of them can be passed to
        start_model_server(overrides=...). An empty list means the server's
        configuration is frozen in its compose file or unit.

    `startable: false` with a `not_startable_reason` is a deliberate record,
    not a bug: several entries kept their weights but lost their runtime in the
    2026-08-11..14 infrastructure consolidation, and the reason says which."""
    return await _request("GET", "/api/v1/infrastructure/model-servers")


@mcp.tool()
async def model_server_utilization() -> Any:
    """How loaded the local model servers are RIGHT NOW — busy vs total slots,
    queue depth and throughput, read live from llama.cpp.

    list_model_servers() says how many slots should exist; this says how many
    are busy. Use it when the local model "feels slow", before starting another
    server, or when deciding whether to spawn more concurrent work.

    `saturated` is the field to watch, not `slot_utilisation`. Every slot busy
    is FINE — each consumer has its own slot by design. Saturated means requests
    are QUEUING (`requests_deferred > 0`), and a queued request lands in whichever
    slot frees first rather than the one holding its prefix, so sustained
    saturation is how warm caches quietly decay into a cold prefill per turn.

    `saturated: null` means unknown, not false: the server was launched without
    `--metrics`, so queue depth and throughput are unreadable (`metrics_hint`
    says so). Missing data is reported as missing rather than as zero.

    `declared_*` vs the live values is a drift check — they disagree exactly
    when a unit file was edited but the server never restarted."""
    return await _request("GET", "/api/v1/infrastructure/model-servers/utilization")


@mcp.tool()
async def get_llm_route() -> Any:
    """Which local model currently answers as 'the local model', and whether
    that is pinned or auto-selected.

    Also answers "what model am I running on?" — the payload carries `model_id`
    (the loaded model as the backend reports it, read live) and a ready-to-say
    `summary`. A dedicated which_model_am_i() wrapper existed briefly in Aug 2026
    and was removed: it cost ~190 tokens of prompt prefix on every request, and
    became redundant once Hermes started naming real models instead of the
    `aria-resident` alias.

    This is the model YOU are most likely running on: Hermes's default provider
    is ARIA's /llm/v1 passthrough, so whatever this reports as `serving` is what
    is generating your replies. `loaded` lists every resident server — more than
    one can be up, and you can address a specific one by name."""
    return await _request("GET", "/api/v1/infrastructure/llm-route")


@mcp.tool()
async def set_llm_route(slug: Optional[str] = None) -> dict:
    """Pin 'the local model' to one server, or pass slug=None for auto (follow
    whichever is resident, largest first).

    Because Hermes follows the same passthrough, this changes the model backing
    your own replies from the next turn onward — no gateway restart. Refuses
    (409) if the named server is not running."""
    return await _request("PUT", "/api/v1/infrastructure/llm-route", json={"slug": slug})


@mcp.tool()
async def list_gpu_devices() -> Any:
    """The physical GPUs on corsair-ai and the memory pools they own.

    There are TWO, with separate memory: the Strix Halo iGPU (124 GiB of shared
    system memory) and a discrete Radeon AI PRO R9700 (32 GiB of its own VRAM).
    A model on one does NOT compete with a model on the other — running one from
    each simultaneously is a supported deployment, not an accident. Read this
    before concluding "the box is full": the answer depends on which pool.

    `spilling: true` on the R9700 means it is serving out of system RAM, at
    which point the pools are no longer independent."""
    return await _request("GET", "/api/v1/infrastructure/model-servers/devices")


@mcp.tool()
async def start_model_server(
    slug: str, force: bool = False, overrides: Optional[dict] = None
) -> dict:
    """Start a model server by its registry slug, optionally choosing HOW it loads.

    `overrides` selects launch parameters — device placement, context size, KV
    cache type, drafter, slot count — keyed by the `parameters[].name` values
    list_model_servers() reports for that server. Only servers that expose
    `parameters` accept them; compose-frozen ones refuse (409) rather than
    silently ignoring the request. Examples:

        start_model_server("DS4-0731-IQ3_XXS-Halo-Vulkan",
                           overrides={"kv": "q8_0", "draft": "none"})
        start_model_server("DS4-0731-IQ3_S-Hybrid-ROCm-Dual",
                           overrides={"placement": "split", "ctx": "65536"})

    Omitting `overrides` starts with the deployment's own defaults AND clears
    any override a previous start applied — a plain start is always a clean one.

    Refuses (409) if a mutually-exclusive server is running, if the port is
    taken, or if the projected footprint would blow the safety margin of THAT
    SERVER'S memory pool (the Halo's shared RAM or the R9700's VRAM — see
    list_gpu_devices). Pass force=True only if you have verified it is safe.

    REMOTE SERVERS (2026-08-15): 'Red-Qwen3.6-35B-A3B' (RTX 5090) and
    'Ridge-Qwen3.8-27B' (RTX 3090) can now be started from here. Starting
    one WAKES the machine if it is asleep, then starts its model service, then
    waits until it actually serves — so the call can take a few minutes
    (RED ~5 min worst case, Ridge ~90s cold) and returns state='ready' only
    when health confirms it. state='starting' means the command was issued but
    it was not serving yet; that is a real outcome, not an error. Remote servers
    do not accept `overrides` (their parameters live on the remote host).

    Their states are meaningful: 'asleep' (box down), 'stopped' (box up, model
    not serving — start it), 'running' (serving)."""
    # Longer per-request timeout than the global default: a cold
    # `docker compose up -d` (image build/pull, container recreate) can far
    # exceed 20s, and timing out client-side while the start proceeds
    # server-side reads as a false failure.
    body: dict = {"force": force}
    if overrides:
        body["overrides"] = overrides
    # 180s covered local starts. A REMOTE start can legitimately take far
    # longer: it wakes the machine (deadline 240s) and then waits for the model
    # service to serve (up to 300s on RED) — 540s worst case. A client timeout
    # shorter than the server's own deadlines is the worst combination, because
    # the operation SUCCEEDS while the caller is told it failed, and Hermes
    # would then retry a start that is already running.
    return await _request(
        "POST", f"/api/v1/infrastructure/model-servers/{slug}/start",
        json=body, timeout=600.0,
    )


@mcp.tool()
async def stop_model_server(slug: str) -> dict:
    """Stop a model server by its registry slug.

    For remote servers (RED, Ridge) this stops the MODEL SERVICE and leaves the
    machine awake — use sleep_model_server to suspend the box itself."""
    # Remote stop does an ssh round trip plus a health re-check; a sleeping box
    # costs a connect timeout first, so the 20s default is too tight.
    return await _request(
        "POST", f"/api/v1/infrastructure/model-servers/{slug}/stop", timeout=120.0,
    )


@mcp.tool()
async def bind_model_server(slug: str, agent: str, force: bool = False) -> dict:
    """Record that `agent` (slug or id) is powered by model server `slug` —
    descriptive bookkeeping, not a routing change. Refuses (409) if that
    server is already bound to a different agent; pass force=True to add an
    extra slot for a rare case outside ARIA's normal one-agent-per-server rule."""
    return await _request(
        "POST", f"/api/v1/infrastructure/model-servers/{slug}/bind",
        json={"agent": agent, "force": force},
    )


@mcp.tool()
async def unbind_model_server(agent: str) -> dict:
    """Clear whatever model-server binding `agent` (slug or id) currently has."""
    return await _request("POST", "/api/v1/infrastructure/model-servers/unbind", json={"agent": agent})


@mcp.tool()
async def sleep_model_server(slug: str) -> dict:
    """Suspend an off-box MACHINE — 'Ridge-Qwen3.8-27B' or 'Red-Qwen3.6-35B-A3B'.

    This suspends the whole box, which is NOT the same as stopping its model:
    use stop_model_server to free the GPU while leaving the machine up, and
    this only when you want the machine itself asleep. Noops with state=asleep
    if it is already unreachable.

    Waking is handled for you — start_model_server wakes the box first, and the
    wake proxies also wake it on an inference request."""
    return await _request("POST", f"/api/v1/infrastructure/model-servers/{slug}/sleep")


# --- Non-LLM services + the ontology graph -----------------------------------
# Two separate registries on purpose (a merged one would make "mongod is down"
# read as "stopped on purpose" and silence the alert — see
# api/aria/infrastructure/services.py). `whats_running` is the union read, so
# there is still one question to ask.


@mcp.tool()
async def whats_running() -> Any:
    """What is actually running on corsair-ai, across BOTH registries.

    Use this for "is everything up?", "what's running?", "is X running?".
    Returns non-LLM services (mongod, embeddings, hermes-gateway, signal-cli,
    samba, ...) plus the local model servers, and — the useful part — an
    `unhealthy` list containing ONLY services that are expected to be up and
    are not. A stopped model server is normal (they are mutually RAM-exclusive)
    and a stopped on_demand service is normal, so neither is flagged."""
    return await _request("GET", "/api/v1/infrastructure/running")


@mcp.tool()
async def list_services() -> Any:
    """Every non-LLM service with its live state, `expected_state`
    (always_up | on_demand) and health verdict. `needs_review=true` marks
    entries whose expected_state was inferred, not confirmed by Ben."""
    return await _request("GET", "/api/v1/infrastructure/services")


@mcp.tool()
async def start_service(slug: str) -> dict:
    """Start a non-LLM service by slug (e.g. 'aria-stt'). Refuses with 409 for
    unmanageable entries: aria-api (would restart itself mid-request),
    aria-tmux (killing it can orphan every watched claude-* session) and
    system units ARIA has no root for."""
    return await _request("POST", f"/api/v1/infrastructure/services/{slug}/start")


@mcp.tool()
async def stop_service(slug: str) -> dict:
    """Stop a non-LLM service by slug. Same unmanageable refusals as
    start_service."""
    return await _request("POST", f"/api/v1/infrastructure/services/{slug}/stop")


@mcp.tool()
async def kg_search(query: str, type: Optional[str] = None, limit: int = 10) -> Any:
    """Semantic search over the ONTOLOGY GRAPH of Ben's world — machines,
    services, projects, datastores, networks, devices.

    Use this for STRUCTURAL questions ("what is the gaming PC used for
    inference?", "where do backups go?") where search_memory would only find
    whatever someone happened to write in prose. Optional `type` filter:
    machine | device | service | project | datastore | network |
    external_service | person."""
    body: dict = {"query": query, "limit": limit}
    if type:
        body["type"] = type
    return await _request("POST", "/api/v1/ontology/search", json=body)


@mcp.tool()
async def kg_entity(slug: str) -> Any:
    """One entity plus every structural edge in and out of it (its
    neighborhood). Slugs are `type:name` — e.g. 'machine:corsair-ai',
    'service:shared-mongod', 'project:aria'.

    Answers "what runs on X?", "what depends on X?", "what is X?". Most
    attributes are DERIVED from ARIA's registries and db.projects, so they
    reflect what is actually configured rather than what was documented."""
    return await _request("GET", f"/api/v1/ontology/{slug}")


@mcp.tool()
async def kg_map(type: Optional[str] = None) -> Any:
    """Typed overview of the graph — all machines, all services, all projects.
    Omit `type` for everything."""
    params = {"type": type} if type else None
    return await _request("GET", "/api/v1/ontology/map", params=params)


@mcp.tool()
async def kg_memories(slug: str, limit: int = 25) -> Any:
    """Memories that refer to a given entity — the graph -> memory direction.

    Complements search_memory: that searches prose semantically, this returns
    everything linked to a specific thing."""
    return await _request(
        "GET", f"/api/v1/ontology/entity/{slug}/memories", params={"limit": limit}
    )


@mcp.tool()
async def pull_model(
    repo_id: str, filename: str, name: str, runtime: str,
    port: Optional[int] = None, ctx: int = 32768,
) -> dict:
    """Download a GGUF from Hugging Face and provision it as a startable model
    server. `runtime` picks the llama.cpp build: mainline-vulkan | mainline-cpu
    | rocmfp4-fork | rocmfpx-vulkan-fork — standard GGUFs take mainline;
    ROCmFP4/FP6/FPX files need their matching fork. Returns a job id; poll
    list_model_pulls (20-60 GB, many minutes)."""
    body: dict[str, Any] = {
        "repo_id": repo_id, "filename": filename, "name": name, "runtime": runtime, "ctx": ctx,
    }
    if port is not None:
        body["port"] = port
    return await _request("POST", "/api/v1/infrastructure/model-servers/pull", json=body)


@mcp.tool()
async def list_model_pulls() -> Any:
    """Recent model-pull jobs with status (downloading/wiring/completed/failed),
    log tail, and a stale flag (aria-api restarted mid-pull)."""
    return await _request("GET", "/api/v1/infrastructure/model-servers/pulls")


# ──────────────────────────────────────────────────────────────────── memory ──

@mcp.tool()
async def search_memory(query: str, limit: int = 10, content_type: Optional[str] = None) -> Any:
    """Hybrid (vector + lexical) search over ARIA's long-term memory.
    content_type optionally filters: fact | preference | event | skill | document."""
    body: dict[str, Any] = {"query": query, "limit": limit}
    if content_type:
        body["content_type"] = content_type
    return await _request("POST", "/api/v1/memories/search", json=body)


@mcp.tool()
async def add_memory(
    content: str,
    content_type: str = "fact",
    categories: Optional[list[str]] = None,
    importance: float = 0.5,
) -> dict:
    """Store a durable memory. content_type: fact | preference | event | skill |
    document. importance 0.0–1.0."""
    body: dict[str, Any] = {
        "content": content,
        "content_type": content_type,
        "importance": importance,
    }
    if categories:
        body["categories"] = categories
    return await _request("POST", "/api/v1/memories", json=body)


@mcp.tool()
async def retrieval_capabilities() -> dict:
    """Are mongot (search) and the embeddings model currently in use, and what
    is waiting to be re-embedded?

    Returns each switch with the reason it was last flipped, `retrieval_mode`
    (hybrid | lexical | fallback — what a memory search will actually do right
    now), the backing containers' state, and the backfill backlog. Check this
    first when recall looks worse than expected: `fallback` means mongot is off
    and results come from a crude mongod scan, not from search."""
    return await _request("GET", "/api/v1/capabilities/retrieval")


@mcp.tool()
async def set_retrieval_capabilities(
    embeddings: Optional[bool] = None,
    search: Optional[bool] = None,
    reason: str = "",
    with_service: bool = False,
) -> dict:
    """Turn the embeddings model and/or mongot off (or back on) WITHOUT
    stopping ARIA. Omitted switches are left alone.

    Turning them off degrades retrieval, it does not break writes: memories are
    still stored, flagged `embedding_pending`, and re-embedded automatically the
    moment `embeddings` goes back to true — no repair step. With `search` off,
    recall falls back to a mongod-native scan. Health checks stop paging about a
    capability that is off on purpose.

    `with_service=true` also stops/starts the backing container
    (shared-embeddings / shared-mongot) — use it to actually free the box, in
    the safe order (switch off then stop; start then switch on)."""
    body: dict[str, Any] = {"reason": reason, "with_service": with_service,
                            "changed_by": "hermes"}
    if embeddings is not None:
        body["embeddings"] = embeddings
    if search is not None:
        body["search"] = search
    return await _request("PUT", "/api/v1/capabilities/retrieval", json=body)


# ───────────────────────────────────────────────── coding sessions (sub-agents) ──
# ARIA-spawned Claude/Codex coding agents — same substrate as watched shells, but
# launched and lifecycle-managed by ARIA (watchdog/checkpoints).

@mcp.tool()
async def list_coding_sessions(status: Optional[str] = None) -> Any:
    """List ARIA-spawned coding sessions (sub-agents). status optionally filters
    (queued|running|completed|failed|stopped). NOTE 'queued' is real: a spawn
    past coding_max_concurrent_sessions waits for a free slot, so polling only
    for 'running' makes a queued session look like it failed. Failed sessions
    now carry an `error` field with the reason."""
    params = {"status": status} if status else None
    return await _request("GET", "/api/v1/coding/sessions", params=params)


@mcp.tool()
async def create_coding_session(
    workspace: str,
    prompt: str,
    backend: Optional[str] = None,
    loop: bool = False,
    host: Optional[str] = None,
    subagent_profile: Optional[str] = None,
    create_worktree: Optional[bool] = None,
) -> dict:
    """Spawn a coding sub-agent in `workspace` with an initial `prompt`.

    backend: 'claude_code' (default) | 'codex' | 'pi-code' (alias 'pi') |
        'pool' (Poolside's agent against local Laguna; aliases 'pool-cli',
        'poolside').
    loop=True: watchdog nudges on idle until RALPH_DONE or the nudge/deadline
        caps. Toggle later with set_coding_loop.
    host: an aria-node id to run remotely; omit for this host.
    subagent_profile: a db.agents slug whose backend/model/role apply; an
        explicit backend still wins.
    create_worktree: leave unset. None means "use the configured default",
        which is TRUE — the session gets its own git worktree, ARIA makes its
        checkpoint commits, and it can be rolled back. Passing False opts out of
        the guard for that session and is a deliberate act, not a shortcut.

    Returns immediately (queued/running) — it does NOT wait. Short task: call
    wait_for_coding_session next to block for the result rather than making the
    human poll. Long or looped: check back with get_coding_session."""
    body: dict[str, Any] = {"workspace": workspace, "prompt": prompt}
    if backend:
        body["backend"] = backend
    if loop:
        body["loop"] = {}  # server defaults fill in the loop config
    if host:
        body["host"] = host
    if create_worktree is not None:
        body["create_worktree"] = create_worktree
    if subagent_profile:
        body["subagent_profile"] = subagent_profile
    return await _request("POST", "/api/v1/coding/sessions", json=body)


@mcp.tool()
async def list_nodes() -> Any:
    """List the machines in the fleet (aria-node agents) with online/offline
    status. Use a node's id as the `host` for create_coding_session to run work
    on that machine (e.g. a MacBook for iOS builds)."""
    return await _request("GET", "/api/v1/nodes")


@mcp.tool()
async def get_coding_session(session_id: Optional[str] = None, id: Optional[str] = None) -> dict:
    """One coding sub-agent's structured status: status, backend/model,
    workspace, routing, `error`, `result_summary` (once terminal), and
    `gate_runs` (Verification Gate history, empty if off). Prefer over
    list_coding_sessions when you have the id, and over get_coding_output when
    you want a verdict not raw terminal text. Accepts `session_id` or `id`.
    """
    return await _request("GET", f"/api/v1/coding/sessions/{_one_id(session_id, id, 'session_id')}")


@mcp.tool()
async def wait_for_coding_session(session_id: Optional[str] = None, timeout_seconds: float = 60.0, id: Optional[str] = None) -> dict:
    """Block until a coding sub-agent is terminal (completed/failed/stopped)
    or timeout_seconds elapses (clamped to [1,300]), returning it with
    `result_summary`. Use right after create_coding_session for short tasks
    instead of handing the human an id to poll. timed_out=true means still
    running — check back with get_coding_session rather than re-waiting (looped
    or long tasks always time out here). Accepts `session_id` or `id`.
    """
    return await _request(
        "GET",
        f"/api/v1/coding/sessions/{_one_id(session_id, id, 'session_id')}/wait",
        params={"timeout": timeout_seconds},
        timeout=timeout_seconds + 15,
    )


@mcp.tool()
async def get_coding_diff(session_id: Optional[str] = None, id: Optional[str] = None) -> dict:
    """Get the working-tree diff a coding sub-agent has produced so far in its
    workspace. Use this to summarize what a session actually changed instead
    of paraphrasing raw terminal scrollback from get_coding_output. Takes the id under either `session_id` or `id` — list_coding_sessions returns it as `id`.
    """
    return await _request("GET", f"/api/v1/coding/sessions/{_one_id(session_id, id, 'session_id')}/diff")


@mcp.tool()
async def get_coding_output(session_id: Optional[str] = None, lines: int = 100, id: Optional[str] = None) -> Any:
    """Read recent output from a coding sub-agent. Takes the id under either `session_id` or `id` — list_coding_sessions returns it as `id`.
    """
    return await _request(
        "GET", f"/api/v1/coding/sessions/{_one_id(session_id, id, 'session_id')}/output", params={"lines": lines}
    )


@mcp.tool()
async def send_to_coding_session(text: str, session_id: Optional[str] = None, id: Optional[str] = None) -> dict:
    """Send input/instructions to a running coding sub-agent. Takes the id under either `session_id` or `id` — list_coding_sessions returns it as `id`.
    """
    return await _request(
        "POST", f"/api/v1/coding/sessions/{_one_id(session_id, id, 'session_id')}/input", json={"text": text}
    )


@mcp.tool()
async def stop_coding_session(session_id: Optional[str] = None, id: Optional[str] = None) -> dict:
    """Stop a running coding sub-agent. Takes the id under either `session_id` or `id` — list_coding_sessions returns it as `id`.
    """
    return await _request("POST", f"/api/v1/coding/sessions/{_one_id(session_id, id, 'session_id')}/stop")


@mcp.tool()
async def set_coding_loop(
    session_id: str,
    enabled: bool,
    nudge_prompt: Optional[str] = None,
    nudge_prompt_file: Optional[str] = None,
    done_regex: Optional[str] = None,
    idle_seconds: Optional[int] = None,
    max_nudges: Optional[int] = None,
    deadline_minutes: Optional[int] = None,
    notify_every: Optional[int] = None,
    gate_command: Optional[str] = None,
    gate_timeout: Optional[int] = None,
    gate_max_retries: Optional[int] = None,
) -> dict:
    """Turn the Ralph loop on/off for a running coding sub-agent.

    enabled=True: the watchdog nudges the session whenever it idles (re-checking
    the killswitch each nudge) until it emits `done_regex` (default RALPH_DONE)
    or hits max_nudges/deadline_minutes. enabled=False stops nudging, session
    stays alive. Unset options use the server's coding_loop_* defaults.
    `nudge_prompt_file` is re-read every nudge, so editing it steers a live run.

    Verification Gate (only if the server has coding_gate_enabled — off by
    default): on the done token, a check command runs in the workspace first; a
    failure re-nudges with its output, up to gate_max_retries, then alerts.
    `gate_command` overrides the project/server check for this session. No check
    configured anywhere = skipped, not blocked. History: get_coding_session's
    `gate_runs`."""
    body: dict[str, Any] = {"enabled": enabled}
    for key, val in (
        ("nudge_prompt", nudge_prompt),
        ("nudge_prompt_file", nudge_prompt_file),
        ("done_regex", done_regex),
        ("idle_seconds", idle_seconds),
        ("max_nudges", max_nudges),
        ("deadline_minutes", deadline_minutes),
        ("notify_every", notify_every),
        ("gate_command", gate_command),
        ("gate_timeout", gate_timeout),
        ("gate_max_retries", gate_max_retries),
    ):
        if val is not None:
            body[key] = val
    return await _request("POST", f"/api/v1/coding/sessions/{_one_id(session_id, id, 'session_id')}/loop", json=body)


# ───────────────────────────────────────────────────────────── workflows ──
# Multi-step / fan-out orchestration: a linear DAG of steps plus `parallel`,
# `map`, `code_session` (await:true to join), and `synthesize` actions.

@mcp.tool()
async def list_workflows() -> Any:
    """List saved workflow definitions."""
    return await _request("GET", "/api/v1/workflows")


@mcp.tool()
async def create_workflow(
    name: str,
    steps: list,
    description: str = "",
    tags: Optional[list] = None,
) -> dict:
    """Create a workflow. `steps` is a list of {action, params, depends_on?}.
    Fan-out actions: `parallel` (params.steps = sub-steps, params.max_concurrent),
    `map` (params.over = list/interpolation, params.template = one sub-step, with
    {{item}}/{{index}} in the template), `code_session` (params.await=true joins
    the spawned sub-agent and captures result_summary), and `synthesize`
    (params.inputs or params.from_steps + params.instruction, optional
    backend/model) to reduce prior results into one answer. Reference earlier
    results with {{steps.N.path}} and nested fan-out results with
    {{steps.N.results.M.path}}.

    WARNING: params.backend means DIFFERENT things per action.
    - `code_session`: a coding SUBSTRATE — claude_code|codex|pi-code|pool.
    - `synthesize`: an LLM ADAPTER — llamacpp|agentic|ridge|anthropic|openai|
      openrouter|fireworks.
    Passing one vocabulary where the other is expected is invalid."""
    body: dict[str, Any] = {"name": name, "description": description, "steps": steps}
    if tags:
        body["tags"] = tags
    return await _request("POST", "/api/v1/workflows", json=body)


@mcp.tool()
async def run_workflow(workflow_id: Optional[str] = None, dry_run: bool = False, id: Optional[str] = None) -> dict:
    """Run a saved workflow. Returns {run_id, task_id}; poll get_workflow_status
    for step results. dry_run=True validates + renders params without executing
    the actions."""
    return await _request(
        "POST", f"/api/v1/workflows/{_one_id(workflow_id, id, 'workflow_id')}/run", json={"dry_run": dry_run}
    )


@mcp.tool()
async def get_workflow_status(workflow_id: Optional[str] = None, id: Optional[str] = None) -> dict:
    """Get a workflow definition plus its recent runs (status + step_results)."""
    return await _request("GET", f"/api/v1/workflows/{_one_id(workflow_id, id, 'workflow_id')}/status")


if __name__ == "__main__":
    mcp.run()
