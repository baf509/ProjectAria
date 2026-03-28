"""
ARIA - Task Runner

Purpose: Track and run background tasks with persisted status.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.notifications.service import NotificationService


class TaskRunner:
    """In-process background task runner with Mongo-backed status."""

    def __init__(self, db: AsyncIOMotorDatabase, notification_service: NotificationService):
        self.db = db
        self.notification_service = notification_service
        self._tasks: dict[str, asyncio.Task] = {}
        self._recovery_handlers: dict[str, Callable[[dict], Awaitable[object]]] = {}

    async def submit_task(
        self,
        *,
        name: str,
        coroutine_factory: Callable[[], Awaitable[object]],
        notify: bool = True,
        metadata: Optional[dict] = None,
        timeout_seconds: Optional[int] = None,
    ) -> str:
        task_id = str(uuid4())
        now = datetime.now(timezone.utc)
        timeout = timeout_seconds or settings.task_default_timeout_seconds
        metadata_doc = metadata or {}

        await self.db.background_tasks.insert_one(
            {
                "_id": task_id,
                "name": name,
                "status": "pending",
                "progress": 0,
                "result": None,
                "error": None,
                "metadata": metadata_doc,
                "timeout_seconds": timeout,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
        )
        self._launch_task(
            task_id=task_id,
            name=name,
            coroutine_factory=coroutine_factory,
            notify=notify,
            timeout_seconds=timeout,
        )
        return task_id

    def register_recovery_handler(
        self,
        task_kind: str,
        handler: Callable[[dict], Awaitable[object]],
    ) -> None:
        """Register a restart recovery handler for a persisted task kind."""
        self._recovery_handlers[task_kind] = handler

    def _launch_task(
        self,
        *,
        task_id: str,
        name: str,
        coroutine_factory: Callable[[], Awaitable[object]],
        notify: bool,
        timeout_seconds: int,
    ) -> None:

        async def runner():
            await self.db.background_tasks.update_one(
                {"_id": task_id},
                {"$set": {"status": "running", "progress": 5, "updated_at": datetime.now(timezone.utc)}},
            )
            try:
                result = await asyncio.wait_for(coroutine_factory(), timeout=timeout_seconds)
                await self.db.background_tasks.update_one(
                    {"_id": task_id},
                    {
                        "$set": {
                            "status": "completed",
                            "progress": 100,
                            "result": result,
                            "updated_at": datetime.now(timezone.utc),
                            "completed_at": datetime.now(timezone.utc),
                        }
                    },
                )
                if notify:
                    await self.notification_service.notify(
                        source="task",
                        event_type="complete",
                        detail=f"{name} completed",
                    )
            except asyncio.TimeoutError:
                await self.db.background_tasks.update_one(
                    {"_id": task_id},
                    {
                        "$set": {
                            "status": "failed",
                            "error": f"Timed out after {timeout_seconds} seconds",
                            "updated_at": datetime.now(timezone.utc),
                            "completed_at": datetime.now(timezone.utc),
                        }
                    },
                )
                if notify:
                    await self.notification_service.notify(
                        source="task",
                        event_type="error",
                        detail=f"{name} timed out",
                    )
            except asyncio.CancelledError:
                await self.db.background_tasks.update_one(
                    {"_id": task_id},
                    {
                        "$set": {
                            "status": "cancelled",
                            "updated_at": datetime.now(timezone.utc),
                            "completed_at": datetime.now(timezone.utc),
                        }
                    },
                )
                raise
            except Exception as exc:
                await self.db.background_tasks.update_one(
                    {"_id": task_id},
                    {
                        "$set": {
                            "status": "failed",
                            "error": str(exc),
                            "updated_at": datetime.now(timezone.utc),
                            "completed_at": datetime.now(timezone.utc),
                        }
                    },
                )
                if notify:
                    await self.notification_service.notify(
                        source="task",
                        event_type="error",
                        detail=f"{name} failed: {exc}",
                    )
            finally:
                self._tasks.pop(task_id, None)

        self._tasks[task_id] = asyncio.create_task(runner())

    async def recover_stale_tasks(self) -> dict:
        """Recover or mark stale in-flight tasks after a restart."""
        stale_tasks = await self.db.background_tasks.find(
            {"status": {"$in": ["pending", "running"]}}
        ).to_list(length=200)
        recovered = 0
        failed = 0
        for task in stale_tasks:
            metadata = task.get("metadata", {})
            task_kind = metadata.get("task_kind")
            handler = self._recovery_handlers.get(task_kind) if task_kind else None
            if handler:
                timeout_seconds = int(task.get("timeout_seconds") or settings.task_default_timeout_seconds)
                self._launch_task(
                    task_id=task["_id"],
                    name=task["name"],
                    coroutine_factory=lambda handler=handler, metadata=metadata: handler(metadata),
                    notify=True,
                    timeout_seconds=timeout_seconds,
                )
                recovered += 1
            else:
                await self.db.background_tasks.update_one(
                    {"_id": task["_id"]},
                    {
                        "$set": {
                            "status": "failed",
                            "error": "Task interrupted by process restart",
                            "updated_at": datetime.now(timezone.utc),
                            "completed_at": datetime.now(timezone.utc),
                        }
                    },
                )
                failed += 1
        return {"recovered": recovered, "failed": failed}

    async def update_task(
        self,
        task_id: str,
        *,
        status: Optional[str] = None,
        progress: Optional[int] = None,
        result: object = None,
        error: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Update persisted task state for a running background task."""
        updates = {"updated_at": datetime.now(timezone.utc)}
        if status is not None:
            updates["status"] = status
        if progress is not None:
            updates["progress"] = max(0, min(100, progress))
        if result is not None:
            updates["result"] = result
        if error is not None:
            updates["error"] = error
        if status in {"completed", "failed", "cancelled"}:
            updates["completed_at"] = datetime.now(timezone.utc)

        if metadata:
            for key, value in metadata.items():
                updates[f"metadata.{key}"] = value

        result_doc = await self.db.background_tasks.update_one(
            {"_id": task_id},
            {"$set": updates},
        )
        return result_doc.matched_count > 0

    async def list_tasks(self, status: Optional[str] = None) -> list[dict]:
        query = {"status": status} if status else {}
        return await self.db.background_tasks.find(query).sort("created_at", -1).to_list(length=200)

    async def get_task(self, task_id: str) -> Optional[dict]:
        return await self.db.background_tasks.find_one({"_id": task_id})

    def get_running_tasks(self) -> list[asyncio.Task]:
        """Return a list of asyncio Tasks that are still running."""
        return [t for t in self._tasks.values() if not t.done()]

    async def cancel_all(self) -> int:
        """Cancel all running tasks. Used by killswitch."""
        count = 0
        for task_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
                count += 1
        return count

    async def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            existing = await self.get_task(task_id)
            return bool(existing and existing.get("status") in {"cancelled", "completed", "failed"})

        task.cancel()
        return True
