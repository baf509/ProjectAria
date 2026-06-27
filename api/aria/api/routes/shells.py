"""
ARIA - Watched Shells Routes

Purpose: REST + SSE API for the watched shells subsystem.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from aria.api.deps import get_shell_service
from aria.config import settings
from aria.shells.models import (
    Shell,
    ShellCreateRequest,
    ShellEvent,
    ShellEventsResponse,
    ShellInput,
    ShellInputResponse,
    ShellListResponse,
    ShellOverviewResponse,
    ShellResizeRequest,
    ShellSnapshot,
    ShellTagsUpdate,
)
from aria.shells.service import (
    ShellAlreadyExistsError,
    ShellNotFoundError,
    ShellService,
    ShellStoppedError,
)
from aria.shells.tmux import TmuxError, TmuxSessionNotFoundError

import logging

logger = logging.getLogger(__name__)


router = APIRouter()


# ----------------------------------------------------------- rate limiting
_INPUT_BUCKETS: dict[str, list[float]] = {}


def _allow_input(name: str) -> bool:
    limit = int(settings.shells_input_rate_limit_per_minute or 0)
    if limit <= 0:
        return True
    now = time.monotonic()
    window = 60.0
    bucket = _INPUT_BUCKETS.setdefault(name, [])
    while bucket and bucket[0] < now - window:
        bucket.pop(0)
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


# ------------------------------------------------------------------- routes

@router.get("/shells", response_model=ShellListResponse)
async def list_shells(
    status: Optional[str] = Query(default=None, description="Comma-separated status filter"),
    service: Annotated[ShellService, Depends(get_shell_service)] = None,
):
    status_filter = None
    if status:
        status_filter = [s.strip() for s in status.split(",") if s.strip()]
    shells = await service.list_shells(status=status_filter)
    return ShellListResponse(shells=shells)


@router.post("/shells", response_model=Shell, status_code=201)
async def create_shell(
    body: ShellCreateRequest,
    service: Annotated[ShellService, Depends(get_shell_service)],
):
    """Create a detached tmux session and register it as a watched shell.

    The session name is prefixed with the configured shells prefix
    (e.g. `claude-`) if not already. By default Claude Code is launched
    inside the new session; pass `launch_claude: false` for a plain shell.
    """
    try:
        shell = await service.create_shell(
            body.name,
            workdir=body.workdir or "",
            launch_claude=body.launch_claude,
            cols=body.cols,
            rows=body.rows,
        )
    except ShellAlreadyExistsError:
        raise HTTPException(
            status_code=409, detail=f"Shell already exists: {body.name}"
        )
    except TmuxError as exc:
        raise HTTPException(status_code=500, detail=f"tmux error: {exc}")
    return shell


@router.get("/shells/overview", response_model=ShellOverviewResponse)
async def shells_overview(
    awaiting: bool = Query(
        default=False,
        description="If true, return only shells currently awaiting input.",
    ),
    service: Annotated[ShellService, Depends(get_shell_service)] = None,
):
    """One-call digest of the watched-shell fleet for agents.

    Each shell includes `idle_seconds`, `awaiting_input` (sitting at an
    interactive prompt past the idle threshold), the matched `prompt_line`,
    and the `last_line` of visible output. Shells awaiting input are listed
    first. This replaces the list-then-snapshot-each-shell loop an agent would
    otherwise run to answer "what's my fleet doing / what needs me?".
    """
    items = await service.fleet_overview()
    if awaiting:
        items = [i for i in items if i["awaiting_input"]]
    return ShellOverviewResponse(
        shells=items,
        active_count=len(items),
        awaiting_count=sum(1 for i in items if i["awaiting_input"]),
    )


@router.get("/shells/search")
async def search_shell_events(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=50, le=500),
    service: Annotated[ShellService, Depends(get_shell_service)] = None,
):
    """Search across shells.

    Returns two sections:
      - `shells`: shells whose name / short_name / project_dir / tags match `q`.
      - `events`: matching lines of captured output (full-text, regex fallback).

    Previously only `events` were searched despite the tool advertising shell
    metadata search; both are now covered.
    """
    regex = {"$regex": re.escape(q), "$options": "i"}

    # --- shell metadata matches ---------------------------------------------
    shell_matches: list[dict] = []
    try:
        scursor = service.shells.find(
            {
                "$or": [
                    {"name": regex},
                    {"short_name": regex},
                    {"project_dir": regex},
                    {"tags": regex},
                ]
            }
        ).sort("last_activity_at", -1).limit(int(limit))
        async for doc in scursor:
            doc.pop("_id", None)
            shell_matches.append(doc)
    except Exception:  # pragma: no cover - defensive
        shell_matches = []

    # --- event (output) matches ---------------------------------------------
    try:
        cursor = (
            service.events.find(
                {"$text": {"$search": q}},
                {"score": {"$meta": "textScore"}},
            )
            .sort([("score", {"$meta": "textScore"})])
            .limit(int(limit))
        )
        events = []
        async for doc in cursor:
            doc.pop("_id", None)
            events.append(doc)
        return {"shells": shell_matches, "events": events}
    except Exception as exc:
        # Fall back to a bounded regex search if the text index is unavailable.
        # Log the underlying error server-side rather than leaking it to clients.
        logger.warning("shells $text search failed, falling back to regex scan: %s", exc)
        cursor = (
            service.events.find({"text_clean": regex})
            .sort("ts", -1)
            .limit(int(limit))
        )
        events = []
        async for doc in cursor:
            doc.pop("_id", None)
            events.append(doc)
        return {
            "shells": shell_matches,
            "events": events,
            "fallback": "regex",
        }


@router.get("/shells/{name}", response_model=Shell)
async def get_shell(
    name: str,
    service: Annotated[ShellService, Depends(get_shell_service)],
):
    shell = await service.get_shell(name)
    if not shell:
        raise HTTPException(status_code=404, detail=f"Shell not found: {name}")
    return shell


@router.delete("/shells/{name}", status_code=204)
async def delete_shell(
    name: str,
    service: Annotated[ShellService, Depends(get_shell_service)],
    purge: bool = Query(
        default=False,
        description="If true, also delete the shell row, all events, and snapshots.",
    ),
):
    """Kill a tmux session and mark its shell row stopped (default).

    With `?purge=true`, additionally delete the shell row, all events,
    and snapshots. Use purge for cleanup; default preserves history so
    `GET /shells/search` keeps working across closed sessions.
    """
    shell = await service.get_shell(name)
    if not shell:
        raise HTTPException(status_code=404, detail=f"Shell not found: {name}")
    try:
        if purge:
            await service.purge_shell(name)
        else:
            await service.kill_shell(name)
    except TmuxError as exc:
        raise HTTPException(status_code=500, detail=f"tmux error: {exc}")
    return None


@router.get("/shells/{name}/events", response_model=ShellEventsResponse)
async def list_shell_events(
    name: str,
    since: Optional[datetime] = None,
    since_line: Optional[int] = None,
    before: Optional[datetime] = None,
    limit: int = Query(default=500, le=2000),
    kinds: Optional[str] = Query(default=None),
    service: Annotated[ShellService, Depends(get_shell_service)] = None,
):
    kind_list = None
    if kinds:
        kind_list = [k.strip() for k in kinds.split(",") if k.strip()]
    events = await service.list_events(
        name,
        since=since,
        since_line=since_line,
        before=before,
        limit=limit,
        kinds=kind_list,
    )
    return ShellEventsResponse(events=events, has_more=len(events) >= limit)


@router.get("/shells/{name}/snapshot")
async def get_latest_snapshot(
    name: str,
    service: Annotated[ShellService, Depends(get_shell_service)],
):
    snap = await service.get_last_snapshot(name)
    if not snap:
        raise HTTPException(status_code=404, detail="No snapshot available")
    return snap


@router.get("/shells/{name}/screen")
async def get_current_screen(
    name: str,
    lines: int = Query(default=40, ge=1, le=200),
    service: Annotated[ShellService, Depends(get_shell_service)] = None,
):
    """Capture the shell's visible pane live (fresh, ANSI-stripped, not stored).

    Unlike `/snapshot` (which returns the last worker-stored snapshot, up to
    ~30s stale), this captures the pane right now. Best for "what does the
    screen look like at this moment" after sending input.
    """
    shell = await service.get_shell(name)
    if not shell:
        raise HTTPException(status_code=404, detail=f"Shell not found: {name}")
    try:
        screen = await service.current_screen(name, lines=lines)
    except TmuxError as exc:
        raise HTTPException(status_code=500, detail=f"tmux error: {exc}")
    if screen is None:
        raise HTTPException(status_code=409, detail=f"Shell stopped: {name}")
    return {"name": name, "lines": lines, "screen": screen}


@router.get("/shells/{name}/stream")
async def stream_shell_events(
    request: Request,
    name: str,
    since_line: Optional[int] = None,
    service: Annotated[ShellService, Depends(get_shell_service)] = None,
):
    """SSE stream of shell events. Starts with a catchup fetch then polls."""
    shell = await service.get_shell(name)
    if not shell:
        raise HTTPException(status_code=404, detail=f"Shell not found: {name}")

    async def event_generator():
        last_line = since_line or 0
        # Initial catchup
        catchup = await service.list_events(name, since_line=last_line, limit=500)
        for evt in catchup:
            last_line = max(last_line, evt.line_number)
            yield {
                "event": "shell_event",
                "data": evt.model_dump_json(),
            }
        # Status snapshot
        shell_doc = await service.get_shell(name)
        if shell_doc:
            yield {
                "event": "shell_status",
                "data": json.dumps({
                    "status": shell_doc.status,
                    "last_activity_at": shell_doc.last_activity_at.isoformat(),
                }),
            }
        # Live tail
        heartbeat_every = 15.0
        last_heartbeat = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            batch = await service.list_events(name, since_line=last_line, limit=200)
            for evt in batch:
                last_line = max(last_line, evt.line_number)
                yield {
                    "event": "shell_event",
                    "data": evt.model_dump_json(),
                }
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_every:
                yield {"event": "heartbeat", "data": "{}"}
                last_heartbeat = now
            await asyncio.sleep(0.5)

    return EventSourceResponse(
        event_generator(),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/shells/{name}/input", response_model=ShellInputResponse)
async def send_shell_input(
    name: str,
    body: ShellInput,
    service: Annotated[ShellService, Depends(get_shell_service)],
):
    if not _allow_input(name):
        raise HTTPException(status_code=429, detail="Rate limit exceeded for shell")
    try:
        line, screen = await service.send_input(
            name,
            body.text,
            append_enter=body.append_enter,
            literal=body.literal,
            wait_ms=body.wait_ms,
        )
    except ShellNotFoundError:
        raise HTTPException(status_code=404, detail=f"Shell not found: {name}")
    except ShellStoppedError:
        raise HTTPException(status_code=409, detail=f"Shell stopped: {name}")
    except TmuxError as exc:
        raise HTTPException(status_code=500, detail=f"tmux error: {exc}")
    return ShellInputResponse(ok=True, line_number=line, screen=screen)


@router.post("/shells/{name}/resize", status_code=204)
async def resize_shell(
    name: str,
    body: ShellResizeRequest,
    service: Annotated[ShellService, Depends(get_shell_service)],
):
    """Resize the tmux window for a shell.

    Mobile and widget clients call this on view appear (and on rotation /
    keyboard show-hide) so the running TUI repaints at the client's actual
    geometry. Without this, sessions stay at tmux's 80x24 default and TUIs
    wrap badly on phones.
    """
    try:
        await service.resize_shell(name, body.cols, body.rows)
    except TmuxSessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Shell not found: {name}")
    except TmuxError as exc:
        raise HTTPException(status_code=500, detail=f"tmux error: {exc}")
    return None


@router.post("/shells/{name}/tags", response_model=Shell)
async def set_shell_tags(
    name: str,
    body: ShellTagsUpdate,
    service: Annotated[ShellService, Depends(get_shell_service)],
):
    shell = await service.get_shell(name)
    if not shell:
        raise HTTPException(status_code=404, detail=f"Shell not found: {name}")
    await service.set_tags(name, body.tags)
    refreshed = await service.get_shell(name)
    return refreshed
