"""
ARIA - Node Service

Registry for aria-node agents + ingest of the shell events/snapshots they push.
Ingested data is stamped with the node's id as `host`, so a remote node's shells
appear in the same fleet (`fleet_overview`, scrollback) as local ones — the read
path is already host-agnostic. Driving remote shells goes through the command
queue (see commands.py + ShellService dispatch).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.shells.ansi import strip_ansi
from aria.shells.service import ShellService
from aria.nodes import commands
from aria.nodes.models import EventBatchIn, NodeInfo, NodeRegisterRequest, SnapshotIn


def _now() -> datetime:
    return datetime.now(timezone.utc)


class NodeService:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.nodes = db.nodes
        self.shell_service = ShellService(db)

    # ------------------------------------------------------------- registry
    async def register(self, req: NodeRegisterRequest) -> dict:
        now = _now()
        await self.nodes.update_one(
            {"_id": req.node_id},
            {
                "$set": {
                    "hostname": req.hostname,
                    "os": req.os,
                    "arch": req.arch,
                    "capabilities": req.capabilities,
                    "agent_version": req.agent_version,
                    "last_heartbeat_at": now,
                },
                "$setOnInsert": {"registered_at": now},
            },
            upsert=True,
        )
        return await self.get_node(req.node_id)  # type: ignore[return-value]

    async def heartbeat(self, node_id: str, *, live_shells: Optional[list[str]] = None) -> bool:
        res = await self.nodes.update_one(
            {"_id": node_id}, {"$set": {"last_heartbeat_at": _now()}}
        )
        if res.matched_count > 0 and live_shells is not None:
            live = set(live_shells)
            for name in sorted(live):
                await self.shell_service.register_shell(name, host=node_id)
            cursor = self.db.shells.find({"host": node_id, "status": {"$in": ["active", "idle"]}})
            async for shell in cursor:
                name = shell.get("name")
                if name and name not in live:
                    await self.shell_service.mark_stopped(name)
        return res.matched_count > 0

    def _online(self, doc: dict) -> bool:
        hb = doc.get("last_heartbeat_at")
        if not hb:
            return False
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        return (_now() - hb).total_seconds() < settings.node_heartbeat_timeout_seconds

    def _to_info(self, doc: dict) -> dict:
        return NodeInfo(
            node_id=doc["_id"],
            hostname=doc.get("hostname", ""),
            os=doc.get("os", ""),
            arch=doc.get("arch", ""),
            capabilities=doc.get("capabilities", []),
            agent_version=doc.get("agent_version", ""),
            status="online" if self._online(doc) else "offline",
            registered_at=doc.get("registered_at"),
            last_heartbeat_at=doc.get("last_heartbeat_at"),
        ).model_dump()

    async def get_node(self, node_id: str) -> Optional[dict]:
        doc = await self.nodes.find_one({"_id": node_id})
        return self._to_info(doc) if doc else None

    async def list_nodes(self) -> list[dict]:
        docs = await self.nodes.find().sort("_id", 1).to_list(length=200)
        return [self._to_info(d) for d in docs]

    async def is_online(self, node_id: str) -> bool:
        doc = await self.nodes.find_one({"_id": node_id})
        return bool(doc) and self._online(doc)

    # -------------------------------------------------------------- ingest
    async def ingest_events(self, node_id: str, batch: EventBatchIn) -> int:
        name = batch.shell_name
        await self.shell_service.register_shell(
            name, project_dir=batch.project_dir, host=node_id
        )
        n = 0
        if batch.events:
            n = await self.shell_service.insert_events_batch(
                name, [e.model_dump() for e in batch.events], host=node_id
            )
        if batch.stopped:
            await self.shell_service.mark_stopped(name)
        return n

    async def ingest_snapshot(self, node_id: str, snap: SnapshotIn) -> None:
        name = snap.shell_name
        await self.shell_service.register_shell(name, host=node_id)
        clean = strip_ansi(snap.content)
        h = hashlib.sha256(clean.encode("utf-8", errors="replace")).hexdigest()
        await self.shell_service.insert_snapshot(name, clean, h)

    # ------------------------------------------------- node-facing commands
    async def claim_commands(self, node_id: str) -> list[dict]:
        return await commands.claim_commands(self.db, node_id)

    async def complete_command(
        self, cmd_id: str, *, result: Optional[dict] = None, error: Optional[str] = None
    ) -> bool:
        command = await self.db.shell_commands.find_one({"_id": cmd_id})
        completed = await commands.complete_command(
            self.db, cmd_id, result=result, error=error
        )
        if completed and not error and isinstance(command, dict) and command.get("kind") == "stop":
            args = command.get("args") or {}
            name = args.get("name")
            if name:
                if args.get("purge"):
                    await self.shell_service.delete_shell_history(name)
                else:
                    await self.shell_service.mark_stopped(name)
        return completed
