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
