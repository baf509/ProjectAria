"""
ARIA - Infrastructure Routes

Purpose: Model-server control plane (start/stop/bind the local LLM servers,
pull + provision new ones from Hugging Face).
See aria.infrastructure.model_servers / model_pull for the registry, the
safety gates, and the provisioning pipeline.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_db, get_model_pull_service, get_model_server_manager
from aria.infrastructure.llm_route import (
    backend_model_id as _backend_model_id,
    base_url_for,
    is_servable,
    match_requested,
    read_pin,
    select,
    write_pin,
)
from aria.infrastructure.model_pull import RUNTIME_TEMPLATES, ModelPullService
from aria.infrastructure.model_servers import (
    ModelServerBindingConflict,
    ModelServerError,
    ModelServerManager,
    ModelServerNotFound,
    ModelServerSafetyError,
)

router = APIRouter(prefix="/infrastructure", tags=["infrastructure"])


class StartStopRequest(BaseModel):
    force: bool = False


class BindRequest(BaseModel):
    agent: str
    force: bool = False


class UnbindRequest(BaseModel):
    agent: str


class PullRequest(BaseModel):
    repo_id: str = Field(description="Hugging Face repo, e.g. unsloth/Qwen3.6-27B-MTP-GGUF")
    filename: str = Field(description="Exact .gguf filename inside the repo")
    name: str = Field(description="Registry slug / directory name for the new server")
    runtime: str = Field(description=f"One of {sorted(RUNTIME_TEMPLATES)}")
    port: Optional[int] = Field(default=None, description="Host port; auto-allocated from 8105+ if omitted")
    ctx: int = Field(default=32768, ge=512, le=1048576, description="--ctx-size for the generated service")


class LlmRouteRequest(BaseModel):
    slug: Optional[str] = Field(
        default=None,
        description="Server to serve as the local model; null/'auto' follows whichever is resident",
    )


@router.get("/llm-route")
async def get_llm_route(
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Who currently answers as 'the local model', and whether that is pinned.

    This is the read side of the knob that decides what `LLAMACPP_URL` — and
    therefore Hermes, which follows the same passthrough — actually talks to
    when more than one server is resident.

    Also answers the identity question "what model am I actually running on?".
    Consumers configured against the passthrough see only the synthetic id
    `aria-resident`, so asked what they are they will describe their *config*
    ("aria-resident on a custom provider") rather than the model. `model_id`
    here is the loaded model as the backend itself reports it — ground truth,
    read live from the server rather than from this registry — and `summary`
    is a ready-to-say sentence built from it.
    """
    servers = await manager.status(db)
    pin = await read_pin(db)
    chosen, reason, _ = select(servers, pin=pin)

    model_id = await _backend_model_id(base_url_for(chosen) or "") if chosen else None
    slug = chosen.get("slug") if chosen else None

    if slug:
        # Prefer the backend's own answer; fall back to the registry's filename
        # if the server is up but /v1/models did not answer.
        name = model_id or (chosen.get("model_file") or "").rsplit("/", 1)[-1] or slug
        summary = (
            f"Running on {name} (ARIA registry slug '{slug}', "
            f"{chosen.get('backend_device') or 'local'}), served locally through "
            f"ARIA's /llm/v1 passthrough. 'aria-resident' is the routing alias, "
            f"not the model."
        )
    else:
        summary = "No local model server is currently resident."

    return {
        "pinned": pin,
        "serving": slug,
        # Ground truth from the backend, for "which model am I?".
        "model_id": model_id,
        "model_file": chosen.get("model_file") if chosen else None,
        "backend_device": chosen.get("backend_device") if chosen else None,
        "summary": summary,
        "reason": reason,
        "loaded": [
            {
                "slug": s["slug"],
                "resident_gib": s.get("resident_gib_estimate"),
                "backend_device": s.get("backend_device"),
            }
            for s in servers
            if is_servable(s)
        ],
    }


@router.put("/llm-route")
async def set_llm_route(
    body: LlmRouteRequest,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Pin the local-model route to one server, or clear it back to auto.

    Pinning a *stopped* server is refused: silently accepting it would leave
    every consumer on a different model than the one the UI claims is selected.
    """
    slug = (body.slug or "").strip() or None
    if slug and slug.lower() in {"auto", "aria-resident"}:
        slug = None

    if slug is not None:
        servers = await manager.status(db)
        chosen, stopped = match_requested(servers, slug)
        if chosen is None:
            known = sorted(s["slug"] for s in servers)
            raise HTTPException(
                status_code=409 if stopped else 404,
                detail=(
                    f"{slug} is not running — start it before pinning."
                    if stopped
                    else f"unknown model server '{slug}' (known: {', '.join(known)})"
                ),
            )
        slug = chosen["slug"]

    await write_pin(db, slug)
    return await get_llm_route(manager=manager, db=db)


@router.get("/model-servers")
async def list_model_servers(
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    try:
        return {"servers": await manager.status(db)}
    except ModelServerError as exc:
        # e.g. docker daemon unreachable — surfaced, not masked as not_created
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/model-servers/runtimes")
async def list_runtimes():
    """The runtime templates a pulled model can be provisioned onto."""
    return {
        "runtimes": [
            {"slug": slug, "description": t["description"], "image": t["image"]}
            for slug, t in RUNTIME_TEMPLATES.items()
        ]
    }


@router.get("/model-servers/pulls")
async def list_model_pulls(
    pull_service: ModelPullService = Depends(get_model_pull_service),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    return {"pulls": await pull_service.list_jobs(db)}


@router.post("/model-servers/pull")
async def pull_model(
    body: PullRequest,
    pull_service: ModelPullService = Depends(get_model_pull_service),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Download a GGUF from Hugging Face into infrastructure/models/llm/<name>/,
    generate a compose service on the chosen runtime, and register it as a
    startable model server."""
    try:
        return await pull_service.start_pull(
            db, body.repo_id, body.filename, body.name, body.runtime,
            port=body.port, ctx=body.ctx,
        )
    except ModelServerError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/model-servers/{slug}")
async def get_model_server(
    slug: str,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    try:
        await manager.resolve_spec(slug, db)
        servers = await manager.status(db)
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ModelServerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return next(s for s in servers if s["slug"] == slug)


@router.post("/model-servers/{slug}/start")
async def start_model_server(
    slug: str,
    body: StartStopRequest = StartStopRequest(),
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    try:
        return await manager.start(slug, force=body.force, db=db)
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ModelServerSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ModelServerError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/model-servers/{slug}/stop")
async def stop_model_server(
    slug: str,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    try:
        return await manager.stop(slug, db=db)
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ModelServerSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ModelServerError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/model-servers/{slug}/sleep")
async def sleep_model_server(
    slug: str,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Suspend an off-box machine (Ridge). Wake is automatic via its proxy."""
    try:
        return await manager.sleep(slug, db=db)
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ModelServerSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ModelServerError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/model-servers/{slug}/bind")
async def bind_model_server(
    slug: str,
    body: BindRequest,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    try:
        return await manager.bind(db, slug, body.agent, force=body.force)
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ModelServerBindingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/model-servers/unbind")
async def unbind_model_server(
    body: UnbindRequest,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Clear whatever model_server is currently bound to this agent, if any."""
    try:
        return await manager.unbind(db, body.agent)
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
