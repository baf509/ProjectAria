"""
ARIA - Coding Session Routes

Purpose: Manage external coding-agent subprocess sessions.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aria.agents.review import CodingReviewService
from aria.agents.watchdog import CodingWatchdog
from aria.agents.session import CodingSessionManager
from aria.agents.backends.registry import (
    CodingBackendUnavailableError,
    UnknownCodingBackendError,
)
from aria.agents.estop import EstopManager
from aria.api.deps import (
    get_coding_review_service,
    get_coding_session_manager,
    get_coding_watchdog,
    get_estop_manager,
    require_admin,
)
from aria.db.models import CodingSessionCreate, CodingSessionInput, CodingSessionResponse

router = APIRouter(prefix="/coding/sessions", tags=["coding"])


class SessionDeadlineRequest(BaseModel):
    minutes: int


class SessionResumeRequest(BaseModel):
    workspace: str
    backend: str | None = None
    model: str | None = None


class SessionLoopRequest(BaseModel):
    """Toggle the Ralph loop on a session. enabled=false clears it; the other
    fields (all optional) override the coding_loop_* defaults when enabling."""
    enabled: bool
    nudge_prompt: str | None = None
    nudge_prompt_file: str | None = None
    idle_seconds: int | None = None
    done_regex: str | None = None
    max_nudges: int | None = None
    deadline_minutes: int | None = None
    notify_every: int | None = None
    # Verification Gate (Coherence C1) per-session overrides. Unset falls back
    # to the project's check_command, then coding_gate_* global defaults.
    gate_command: str | None = None
    gate_timeout: int | None = None
    gate_max_retries: int | None = None


class EstopRequest(BaseModel):
    reason: str = "Manual activation"
    auto_thaw: bool = False


def serialize_session(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "backend": doc["backend"],
        # LLM-adapter name (llamacpp/agentic/ridge/...) for pi-code sessions --
        # a DIFFERENT vocabulary from `backend` (see subagent_profile
        # resolution in agents/session.py). Needed to tell a Ridge-backed
        # pi-code session apart from a local one; both share backend="pi-code".
        "llm": doc.get("llm"),
        "model": doc.get("model"),
        "workspace": doc["workspace"],
        "source_repo": doc.get("source_repo"),
        "prompt": doc["prompt"],
        "branch": doc.get("branch"),
        "pid": doc.get("pid"),
        "visible": doc.get("visible", False),
        "tmux_pane_id": doc.get("tmux_pane_id"),
        "shell_name": doc.get("shell_name"),
        "status": doc["status"],
        "host": doc.get("host"),
        "loop_enabled": bool(doc.get("loop_config")),
        "routing": doc.get("routing"),
        "error": doc.get("error"),
        "result_summary": doc.get("result_summary"),
        "gate_runs": doc.get("gate_runs", []),
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
        "completed_at": doc.get("completed_at"),
    }


@router.post("", response_model=CodingSessionResponse, status_code=201)
async def start_coding_session(
    body: CodingSessionCreate,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    try:
        session = await manager.start_session(
            workspace=body.workspace,
            backend=body.backend,
            prompt=body.prompt,
            branch=body.branch,
            model=body.model,
            llm=body.llm,
            loop=body.loop.model_dump(exclude_none=True) if body.loop else None,
            host=body.host,
            subagent_profile=body.subagent_profile,
            create_worktree=body.create_worktree,
            worktree_name=body.worktree_name,
        )
    except CodingBackendUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "coding_backend_unavailable",
                "backend": exc.backend,
                "reason": exc.reason,
                "retryable": exc.retryable,
            },
        ) from exc
    except RuntimeError as exc:
        # start_session also raises RuntimeError for caller-visible refusals:
        # an unknown subagent_profile, the manual killswitch, and the e-stop.
        # Those were reaching the client as a bare 500 with no body, so an
        # agent that mistyped a profile — or hit a live e-stop — got nothing
        # it could act on. 409 Conflict: request is well-formed, the service
        # is refusing in its current state.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnknownCodingBackendError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unknown_coding_backend",
                "requested": exc.requested,
                "valid": exc.valid,
                "aliases": exc.aliases,
                "retryable": False,
            },
        ) from exc
    except ValueError as exc:
        # A bad request argument (unknown backend, unusable workspace) is the
        # CALLER's error, so return 400 with the reason rather than a bare 500.
        # This is not cosmetic: an agent that sent backend="pi" got only
        # "500 Internal Server Error", could not tell what was wrong, retried
        # the same call, then silently fell back to a different backend than
        # the one the user asked for. A 400 carrying "Unknown coding backend:
        # pi. Valid: claude_code, codex, pi-code" is self-correcting.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CodingSessionResponse(**serialize_session(session))


@router.get("", response_model=list[CodingSessionResponse])
async def list_coding_sessions(
    status: str | None = None,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    sessions = await manager.list_sessions(status=status)
    return [CodingSessionResponse(**serialize_session(session)) for session in sessions]


@router.get("/concurrency")
async def coding_concurrency(
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    """Live concurrency-limiter gauge: running (slot-holding) sessions, how many
    are queued waiting for a slot, and the configured cap (0 = unbounded)."""
    return await manager.concurrency_stats()


@router.get("/{session_id}", response_model=CodingSessionResponse)
async def get_coding_session(
    session_id: str,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    session = await manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Coding session not found")
    return CodingSessionResponse(**serialize_session(session))


@router.get("/{session_id}/wait")
async def wait_for_coding_session(
    session_id: str,
    timeout: float = 60.0,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    """Block until the session reaches a terminal state (completed/failed/
    stopped) or `timeout` elapses, then return it with `result_summary`
    attached. Thin wrap of CodingSessionManager.wait_for_session, the same
    join primitive workflow fan-out (`code_session await:true`) already uses
    internally — this just makes it reachable directly, for a caller that
    spawned a session outside a workflow and wants to check in on it without
    manually polling output/status in a loop. Clamped to [1, 300]s so a
    caller can't hold the connection open indefinitely."""
    timeout = min(max(timeout, 1.0), 300.0)
    session = await manager.wait_for_session(session_id, timeout=timeout)
    if not session:
        raise HTTPException(status_code=404, detail="Coding session not found")
    result = serialize_session(session)
    result["timed_out"] = bool(session.get("timed_out", False))
    return result


@router.get("/{session_id}/output")
async def get_coding_output(
    session_id: str,
    lines: int = 50,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    session = await manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Coding session not found")
    return {"session_id": session_id, "output": await manager.get_output(session_id, lines=lines)}


@router.post("/{session_id}/input")
async def send_to_coding_session(
    session_id: str,
    body: CodingSessionInput,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    success = await manager.send_input(session_id, body.text)
    if not success:
        raise HTTPException(status_code=404, detail="Coding session not running")
    return {"session_id": session_id, "sent": True}


@router.post("/{session_id}/stop")
async def stop_coding_session(
    session_id: str,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    success = await manager.stop_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Coding session not found")
    return {"session_id": session_id, "stopped": True}


@router.delete("/{session_id}")
async def delete_coding_session(
    session_id: str,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    try:
        success = await manager.delete_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not success:
        raise HTTPException(status_code=404, detail="Coding session not found")
    return {"session_id": session_id, "deleted": True}


@router.get("/{session_id}/diff")
async def get_coding_diff(
    session_id: str,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    session = await manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Coding session not found")
    return {"session_id": session_id, "diff": await manager.get_diff(session_id)}


@router.post("/{session_id}/review")
async def review_coding_session(
    session_id: str,
    review_service: CodingReviewService = Depends(get_coding_review_service),
):
    try:
        return await review_service.review_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{session_id}/review")
async def get_coding_review(
    session_id: str,
    review_service: CodingReviewService = Depends(get_coding_review_service),
):
    report = await review_service.get_report(session_id)
    if not report:
        raise HTTPException(status_code=404, detail="Review report not found")
    return report


@router.post("/watchdog/start")
async def start_coding_watchdog(
    watchdog: CodingWatchdog = Depends(get_coding_watchdog),
):
    return await watchdog.start()


@router.post("/watchdog/stop")
async def stop_coding_watchdog(
    watchdog: CodingWatchdog = Depends(get_coding_watchdog),
):
    return await watchdog.stop()


@router.get("/watchdog/status")
async def get_coding_watchdog_status(
    watchdog: CodingWatchdog = Depends(get_coding_watchdog),
):
    return watchdog.status()


@router.post("/{session_id}/deadline")
async def set_coding_deadline(
    session_id: str,
    body: SessionDeadlineRequest,
    watchdog: CodingWatchdog = Depends(get_coding_watchdog),
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    session = await manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Coding session not found")
    await watchdog.set_deadline(session_id, body.minutes)
    return {"session_id": session_id, "deadline_minutes": body.minutes}


@router.post("/{session_id}/loop", response_model=CodingSessionResponse)
async def set_coding_loop(
    session_id: str,
    body: SessionLoopRequest,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    """Enable or disable the per-session Ralph loop. When enabled, the watchdog
    nudges the session forward whenever it idles at its prompt, re-checking the
    killswitch/e-stop each nudge, until it emits the done token or hits a cap."""
    config = None
    if body.enabled:
        config = body.model_dump(exclude={"enabled"}, exclude_none=True)
    updated = await manager.set_loop_config(session_id, config)
    if not updated:
        raise HTTPException(status_code=404, detail="Coding session not found")
    return CodingSessionResponse(**serialize_session(updated))


@router.post("/resume")
async def resume_coding_session(
    body: SessionResumeRequest,
    manager: CodingSessionManager = Depends(get_coding_session_manager),
):
    """Resume a crashed session from its checkpoint."""
    session = await manager.resume_session(
        workspace=body.workspace,
        backend=body.backend,
        model=body.model,
    )
    if not session:
        raise HTTPException(status_code=404, detail="No checkpoint found for this workspace")
    return CodingSessionResponse(**serialize_session(session))


@router.post("/estop/activate")
async def activate_estop(
    body: EstopRequest,
    estop: EstopManager = Depends(get_estop_manager),
):
    """Activate the automated emergency stop, freezing agent spawning."""
    state = await estop.activate(
        reason=body.reason,
        triggered_by="api",
        auto_thaw=body.auto_thaw,
    )
    return state.to_dict()


# Same rule as the killswitch: freezing is cheap and reversible, un-freezing
# is the decision. Activation stays open (anything may stop the world).
@router.post("/estop/deactivate", dependencies=[Depends(require_admin)])
async def deactivate_estop(
    estop: EstopManager = Depends(get_estop_manager),
):
    """Deactivate the emergency stop (thaw)."""
    state = await estop.deactivate(reason="manual_api")
    return state.to_dict()


@router.get("/estop/status")
async def get_estop_status(
    estop: EstopManager = Depends(get_estop_manager),
):
    """Get current emergency stop status."""
    state = await estop.get_state()
    return state.to_dict()
