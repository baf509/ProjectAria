"""
ARIA - Watched Shells Routes

Purpose: REST + SSE API for the watched shells subsystem.
"""

from __future__ import annotations

import asyncio
import json
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


@router.get("/shells/search")
async def search_shell_events(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=50, le=500),
    service: Annotated[ShellService, Depends(get_shell_service)] = None,
):
    """Full-text search across shell_events.text_clean."""
    try:
        cursor = (
            service.events.find(
                {"$text": {"$search": q}},
                {"score": {"$meta": "textScore"}},
            )
            .sort([("score", {"$meta": "textScore"})])
            .limit(int(limit))
        )
        out = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(doc)
        return {"events": out}
    except Exception as exc:
        # Fall back to a simple regex search if text index is not available.
        regex = {"$regex": q, "$options": "i"}
        cursor = (
            service.events.find({"text_clean": regex})
            .sort("ts", -1)
            .limit(int(limit))
        )
        out = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(doc)
        return {"events": out, "fallback": "regex", "error": str(exc)}


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
        line = await service.send_input(
            name,
            body.text,
            append_enter=body.append_enter,
            literal=body.literal,
        )
    except ShellNotFoundError:
        raise HTTPException(status_code=404, detail=f"Shell not found: {name}")
    except ShellStoppedError:
        raise HTTPException(status_code=409, detail=f"Shell stopped: {name}")
    except TmuxError as exc:
        raise HTTPException(status_code=500, detail=f"tmux error: {exc}")
    return ShellInputResponse(ok=True, line_number=line)


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
