"""
ARIA - Node Routes

The front door for aria-node agents (remote machines that join the fleet):
registration + heartbeat, event/snapshot ingest, and the pull-based command
queue. All gated by the standard X-API-Key middleware. Mounted under /api/v1.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from aria.api.deps import get_node_service
from aria.nodes.models import (
    CommandOut,
    CommandResultIn,
    EventBatchIn,
    NodeInfo,
    NodeHeartbeatRequest,
    NodeRegisterRequest,
    SnapshotIn,
)
from aria.nodes.service import NodeService

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.get("", response_model=list[NodeInfo])
async def list_nodes(svc: Annotated[NodeService, Depends(get_node_service)]):
    return await svc.list_nodes()


@router.post("/register", response_model=NodeInfo)
async def register_node(
    body: NodeRegisterRequest,
    svc: Annotated[NodeService, Depends(get_node_service)],
):
    return await svc.register(body)


@router.post("/{node_id}/heartbeat")
async def node_heartbeat(
    node_id: str,
    svc: Annotated[NodeService, Depends(get_node_service)],
    body: NodeHeartbeatRequest | None = None,
):
    if not await svc.heartbeat(
        node_id, live_shells=body.live_shells if body is not None else None
    ):
        raise HTTPException(status_code=404, detail="Node not registered")
    return {"node_id": node_id, "ok": True}


@router.post("/{node_id}/events")
async def ingest_events(
    node_id: str,
    body: EventBatchIn,
    svc: Annotated[NodeService, Depends(get_node_service)],
):
    n = await svc.ingest_events(node_id, body)
    return {"node_id": node_id, "ingested": n}


@router.post("/{node_id}/snapshot")
async def ingest_snapshot(
    node_id: str,
    body: SnapshotIn,
    svc: Annotated[NodeService, Depends(get_node_service)],
):
    await svc.ingest_snapshot(node_id, body)
    return {"node_id": node_id, "ok": True}


@router.get("/{node_id}/commands", response_model=list[CommandOut])
async def poll_commands(
    node_id: str, svc: Annotated[NodeService, Depends(get_node_service)]
):
    """Long-poll for this node's pending commands (held server-side until one is
    available or the poll window elapses). Returns claimed commands to execute."""
    cmds = await svc.claim_commands(node_id)
    return [CommandOut(id=c["_id"], kind=c["kind"], args=c.get("args", {})) for c in cmds]


@router.post("/{node_id}/commands/{cmd_id}/result")
async def command_result(
    node_id: str,
    cmd_id: str,
    body: CommandResultIn,
    svc: Annotated[NodeService, Depends(get_node_service)],
):
    if not await svc.complete_command(cmd_id, result=body.result, error=body.error):
        raise HTTPException(status_code=404, detail="Command not found")
    return {"cmd_id": cmd_id, "ok": True}
