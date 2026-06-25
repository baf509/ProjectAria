#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=1.2",
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
  - Alerts (relay) : list_alerts, ack_alert  — ProjectAria queues alerts here and
                     Hermes relays them over Signal, since ProjectAria no longer
                     sends Signal/Telegram directly.

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

    Returns each active/idle shell with: status, idle_seconds, awaiting_input
    (sitting at an interactive prompt waiting for a human), the matched
    prompt_line, and the last_line of output. Shells awaiting input are listed
    first, and `awaiting_count` tells you how many need attention.

    Prefer this over list_shells + per-shell snapshots when answering "what is
    my fleet doing?" or "is anything waiting on me?". Set awaiting_only=True to
    get just the shells blocked on input.
    """
    params = {"awaiting": "true"} if awaiting_only else None
    return await _request("GET", "/api/v1/shells/overview", params=params)


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
    """Health of the ProjectAria stack: database, embeddings, and LLM backends."""
    return await _request("GET", "/api/v1/health")


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
# ProjectAria no longer sends Signal/Telegram itself; it queues alerts here and
# Hermes (which owns the signal-cli daemon) relays them, then acks.

@mcp.tool()
async def list_alerts(unacked_only: bool = True, limit: int = 50) -> dict:
    """List ProjectAria alerts (selfcheck failures, idle shells, weekly report).
    Default returns only un-acked alerts — relay these over Signal then ack
    each one so it isn't sent again."""
    params: dict[str, Any] = {"limit": limit}
    if unacked_only:
        params["unacked_only"] = "true"
    return await _request("GET", "/api/v1/alerts", params=params)


@mcp.tool()
async def ack_alert(alert_id: str) -> dict:
    """Acknowledge an alert by id so it is not relayed again."""
    return await _request("POST", f"/api/v1/alerts/{alert_id}/ack")


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
    orchestrator agent unless you pass agent_slug, e.g. 'search-agent' or
    'pi-coding-agent'). Returns {content, conversation_id, tool_calls, usage} —
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
async def read_conversation(conversation_id: str, message_limit: int = 20) -> dict:
    """Read one conversation including its recent messages."""
    return await _request(
        "GET", f"/api/v1/conversations/{conversation_id}",
        params={"msg_limit": message_limit},
    )


@mcp.tool()
async def list_agents() -> Any:
    """List ARIA's agent personas (orchestrator + delegated agents) with their
    configured model/backend and tools."""
    return await _request("GET", "/api/v1/agents")


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


# ───────────────────────────────────────────────── coding sessions (sub-agents) ──
# ARIA-spawned Claude/Codex coding agents — same substrate as watched shells, but
# launched and lifecycle-managed by ARIA (watchdog/checkpoints).

@mcp.tool()
async def list_coding_sessions(status: Optional[str] = None) -> Any:
    """List ARIA-spawned coding sessions (sub-agents). status optionally filters
    (e.g. 'running')."""
    params = {"status": status} if status else None
    return await _request("GET", "/api/v1/coding/sessions", params=params)


@mcp.tool()
async def create_coding_session(
    workspace: str,
    prompt: str,
    backend: Optional[str] = None,
) -> dict:
    """Spawn a coding sub-agent in `workspace` with an initial `prompt`.
    backend: 'claude_code' (default), 'codex', or 'pi'."""
    body: dict[str, Any] = {"workspace": workspace, "prompt": prompt}
    if backend:
        body["backend"] = backend
    return await _request("POST", "/api/v1/coding/sessions", json=body)


@mcp.tool()
async def get_coding_output(session_id: str, lines: int = 100) -> Any:
    """Read recent output from a coding sub-agent."""
    return await _request(
        "GET", f"/api/v1/coding/sessions/{session_id}/output", params={"lines": lines}
    )


@mcp.tool()
async def send_to_coding_session(session_id: str, text: str) -> dict:
    """Send input/instructions to a running coding sub-agent."""
    return await _request(
        "POST", f"/api/v1/coding/sessions/{session_id}/input", json={"text": text}
    )


@mcp.tool()
async def stop_coding_session(session_id: str) -> dict:
    """Stop a running coding sub-agent."""
    return await _request("POST", f"/api/v1/coding/sessions/{session_id}/stop")


if __name__ == "__main__":
    mcp.run()
