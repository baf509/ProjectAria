"""
ARIA - Coding Session Manager

Purpose: Start, stop, and inspect coding-agent subprocess sessions.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.agents.backends.base import StartParams
from aria.agents.backends.registry import BackendRegistry
from aria.agents.subprocess_mgr import CodingSubprocessManager
from aria.config import settings
from aria.notifications.service import NotificationService


class CodingSessionManager:
    """Manage coding sessions backed by external CLI agents."""

    def __init__(self, db: AsyncIOMotorDatabase, notification_service: NotificationService | None = None):
        self.db = db
        self.registry = BackendRegistry()
        self.process_manager = CodingSubprocessManager()
        self.notification_service = notification_service
        self._watch_tasks: dict[str, asyncio.Task] = {}

    async def start_session(
        self,
        *,
        workspace: str,
        backend: Optional[str],
        prompt: str,
        branch: Optional[str] = None,
        model: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> dict:
        backend_name = backend or settings.coding_default_backend
        selected_backend = self.registry.get(backend_name)
        workspace_path = os.path.abspath(workspace)
        params = StartParams(workspace=workspace_path, prompt=prompt, model=model, branch=branch)
        command = selected_backend.start_command(params)
        session_id = str(uuid4())
        now = datetime.now(timezone.utc)

        doc = {
            "_id": session_id,
            "backend": backend_name,
            "model": model,
            "workspace": workspace_path,
            "prompt": prompt,
            "branch": branch,
            "conversation_id": conversation_id,
            "status": "starting",
            "pid": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        await self.db.coding_sessions.insert_one(doc)

        running = await self.process_manager.spawn(session_id, command)
        await self.db.coding_sessions.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "status": "running",
                    "pid": running.process.pid,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        self._watch_tasks[session_id] = asyncio.create_task(self._watch_session(session_id))
        return await self.get_session(session_id)

    async def stop_session(self, session_id: str) -> bool:
        session = await self.get_session(session_id)
        if not session:
            return False
        stopped = await self.process_manager.stop(session_id)
        if not stopped:
            return False
        await self.db.coding_sessions.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "status": "stopped",
                    "updated_at": datetime.now(timezone.utc),
                    "completed_at": datetime.now(timezone.utc),
                    "exit_code": None,
                }
            },
        )
        watch_task = self._watch_tasks.pop(session_id, None)
        if watch_task is not None:
            watch_task.cancel()
        if self.notification_service:
            try:
                await self.notification_service.notify(
                    source=f"coding:{session_id}",
                    event_type="stopped",
                    detail=f"Stopped coding session in {session['workspace'] if session else 'unknown workspace'}",
                    cooldown_seconds=10,
                )
            except Exception:
                pass
        return True

    async def get_session(self, session_id: str) -> Optional[dict]:
        return await self.db.coding_sessions.find_one({"_id": session_id})

    async def list_sessions(self, status: Optional[str] = None) -> list[dict]:
        query = {"status": status} if status else {}
        return await self.db.coding_sessions.find(query).sort("created_at", -1).to_list(length=200)

    def get_output(self, session_id: str, lines: int = 50) -> str:
        return self.process_manager.get_output(session_id, lines=lines)

    async def send_input(self, session_id: str, text: str) -> bool:
        return await self.process_manager.send_input(session_id, text)

    async def get_diff(self, session_id: str) -> str:
        session = await self.get_session(session_id)
        if not session:
            return ""
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            session["workspace"],
            "diff",
            "--no-ext-diff",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        return output or error

    async def _watch_session(self, session_id: str) -> None:
        try:
            exit_code = await self.process_manager.wait(session_id)
            if exit_code is None:
                return

            session = await self.get_session(session_id)
            if session and session.get("status") == "stopped":
                return

            await self.db.coding_sessions.update_one(
                {"_id": session_id},
                {
                    "$set": {
                        "status": "completed" if exit_code == 0 else "failed",
                        "updated_at": datetime.now(timezone.utc),
                        "completed_at": datetime.now(timezone.utc),
                        "exit_code": exit_code,
                    }
                },
            )
            if self.notification_service:
                try:
                    await self.notification_service.notify(
                        source=f"coding:{session_id}",
                        event_type="completed" if exit_code == 0 else "error",
                        detail=f"Session exited with code {exit_code}",
                        cooldown_seconds=10,
                    )
                except Exception:
                    pass
            session = await self.get_session(session_id)
            if session and session.get("conversation_id"):
                summary = f"Coding session {session_id} finished with status {'completed' if exit_code == 0 else 'failed'}."
                try:
                    conv_oid = ObjectId(session["conversation_id"])
                except Exception:
                    return
                await self.db.conversations.update_one(
                    {"_id": conv_oid},
                    {
                        "$push": {
                            "messages": {
                                "id": str(uuid4()),
                                "role": "assistant",
                                "content": summary,
                                "created_at": datetime.now(timezone.utc),
                                "memory_processed": False,
                            }
                        },
                        "$set": {"updated_at": datetime.now(timezone.utc)},
                        "$inc": {"stats.message_count": 1},
                    },
                )
        except asyncio.CancelledError:
            return
        finally:
            self._watch_tasks.pop(session_id, None)
