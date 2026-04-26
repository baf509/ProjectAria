"""
ARIA - Planning Service (tasks + projects)

CRUD for the to-do list and project tracker, plus the dedup helpers used by
the ambient TaskExtractor. Intentionally thin: the service owns persistence
shape and idempotency rules; the extractor owns the LLM call.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.planning.models import (
    Project,
    ProjectActivity,
    ProjectCreateRequest,
    ProjectStatus,
    ProjectUpdateRequest,
    Task,
    TaskCreateRequest,
    TaskSource,
    TaskStatus,
    TaskUpdateRequest,
)

logger = logging.getLogger(__name__)

# Caps to prevent unbounded growth from ambient updates.
MAX_RECENT_ACTIVITY = 20
MAX_NEXT_STEPS = 5
# Tasks with these statuses are "open" — dedup checks against this set so a
# completed task with the same title can be re-created.
OPEN_STATUSES: tuple[TaskStatus, ...] = ("proposed", "active")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_title(title: str) -> str:
    """Lowercase, collapse whitespace, strip terminal punctuation. Used for
    hash-based dedup so trivial wording differences don't create duplicates."""
    s = title.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.rstrip(" .!?;,:")
    return s


def _content_hash(title: str) -> str:
    return hashlib.sha256(_normalize_title(title).encode("utf-8")).hexdigest()


def _slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "project"


def _safe_object_id(value: str) -> Optional[ObjectId]:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


class PlanningService:
    """Tasks + projects persistence. Methods are concurrency-safe (Mongo
    handles concurrent writes); dedup is best-effort, not transactional."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.tasks = db.tasks
        self.projects = db.projects

    # ---------------------------------------------------------------- helpers
    def _task_from_doc(self, doc: dict) -> Task:
        doc = dict(doc)
        doc["id"] = str(doc.pop("_id"))
        if doc.get("project_id") is not None and not isinstance(doc["project_id"], str):
            doc["project_id"] = str(doc["project_id"])
        return Task(**doc)

    def _project_from_doc(self, doc: dict) -> Project:
        doc = dict(doc)
        doc["id"] = str(doc.pop("_id"))
        return Project(**doc)

    # ---------------------------------------------------------- task CRUD
    async def create_task(
        self,
        body: TaskCreateRequest,
        *,
        source: Optional[TaskSource] = None,
    ) -> Task:
        """Create a task. `source` defaults to manual when omitted."""
        now = _now()
        src = source or TaskSource(type="manual")
        doc = {
            "title": body.title.strip(),
            "notes": body.notes,
            "status": body.status,
            "due_at": body.due_at,
            "project_id": body.project_id,
            "tags": list(body.tags),
            "source": src.model_dump(),
            "content_hash": _content_hash(body.title),
            "created_at": now,
            "updated_at": now,
            "completed_at": now if body.status == "done" else None,
        }
        result = await self.tasks.insert_one(doc)
        doc["_id"] = result.inserted_id
        return self._task_from_doc(doc)

    async def list_tasks(
        self,
        *,
        status: Optional[list[TaskStatus]] = None,
        project_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[Task]:
        query: dict = {}
        if status:
            query["status"] = {"$in": list(status)}
        if project_id:
            query["project_id"] = project_id
        cursor = self.tasks.find(query).sort([("status", 1), ("updated_at", -1)]).limit(int(limit))
        return [self._task_from_doc(doc) async for doc in cursor]

    async def get_task(self, task_id: str) -> Optional[Task]:
        oid = _safe_object_id(task_id)
        if oid is None:
            return None
        doc = await self.tasks.find_one({"_id": oid})
        return self._task_from_doc(doc) if doc else None

    async def update_task(self, task_id: str, body: TaskUpdateRequest) -> Optional[Task]:
        oid = _safe_object_id(task_id)
        if oid is None:
            return None
        update: dict = body.model_dump(exclude_unset=True)
        if not update:
            return await self.get_task(task_id)
        update["updated_at"] = _now()
        if "title" in update:
            update["title"] = update["title"].strip()
            update["content_hash"] = _content_hash(update["title"])
        if update.get("status") == "done":
            update["completed_at"] = _now()
        elif "status" in update and update["status"] != "done":
            update["completed_at"] = None
        await self.tasks.update_one({"_id": oid}, {"$set": update})
        return await self.get_task(task_id)

    async def set_task_status(self, task_id: str, status: TaskStatus) -> Optional[Task]:
        return await self.update_task(task_id, TaskUpdateRequest(status=status))

    async def delete_task(self, task_id: str) -> bool:
        oid = _safe_object_id(task_id)
        if oid is None:
            return False
        result = await self.tasks.delete_one({"_id": oid})
        return result.deleted_count > 0

    # ---------------------------------------------------------- project CRUD
    async def create_project(self, body: ProjectCreateRequest) -> Project:
        now = _now()
        slug = body.slug or _slugify(body.name)
        # Slug uniqueness — if collision, suffix with -2, -3, ...
        base = slug
        suffix = 1
        while await self.projects.find_one({"slug": slug}):
            suffix += 1
            slug = f"{base}-{suffix}"
        doc = {
            "name": body.name.strip(),
            "slug": slug,
            "summary": body.summary,
            "status": body.status,
            "next_steps": list(body.next_steps)[:MAX_NEXT_STEPS],
            "relevant_paths": list(body.relevant_paths),
            "tags": list(body.tags),
            "recent_activity": [],
            "created_at": now,
            "updated_at": now,
            "last_signal_at": None,
        }
        result = await self.projects.insert_one(doc)
        doc["_id"] = result.inserted_id
        return self._project_from_doc(doc)

    async def list_projects(self, *, status: Optional[ProjectStatus] = None) -> list[Project]:
        query: dict = {}
        if status:
            query["status"] = status
        cursor = self.projects.find(query).sort([("status", 1), ("last_signal_at", -1), ("updated_at", -1)])
        return [self._project_from_doc(doc) async for doc in cursor]

    async def get_project(self, project_id: str) -> Optional[Project]:
        oid = _safe_object_id(project_id)
        if oid is None:
            return None
        doc = await self.projects.find_one({"_id": oid})
        return self._project_from_doc(doc) if doc else None

    async def get_project_by_slug(self, slug: str) -> Optional[Project]:
        doc = await self.projects.find_one({"slug": slug})
        return self._project_from_doc(doc) if doc else None

    async def update_project(self, project_id: str, body: ProjectUpdateRequest) -> Optional[Project]:
        oid = _safe_object_id(project_id)
        if oid is None:
            return None
        update = body.model_dump(exclude_unset=True)
        if not update:
            return await self.get_project(project_id)
        if "next_steps" in update and update["next_steps"] is not None:
            update["next_steps"] = update["next_steps"][:MAX_NEXT_STEPS]
        update["updated_at"] = _now()
        await self.projects.update_one({"_id": oid}, {"$set": update})
        return await self.get_project(project_id)

    async def delete_project(self, project_id: str) -> bool:
        oid = _safe_object_id(project_id)
        if oid is None:
            return False
        result = await self.projects.delete_one({"_id": oid})
        # Detach orphaned tasks (don't delete them — user may still want them)
        await self.tasks.update_many(
            {"project_id": project_id}, {"$set": {"project_id": None, "updated_at": _now()}}
        )
        return result.deleted_count > 0

    async def append_project_activity(
        self, project_id: str, *, source: str, note: str
    ) -> bool:
        """Push a new activity entry, capped at MAX_RECENT_ACTIVITY (FIFO)."""
        oid = _safe_object_id(project_id)
        if oid is None:
            return False
        entry = ProjectActivity(at=_now(), source=source, note=note).model_dump()
        result = await self.projects.update_one(
            {"_id": oid},
            {
                "$push": {
                    "recent_activity": {
                        "$each": [entry],
                        "$slice": -MAX_RECENT_ACTIVITY,
                    }
                },
                "$set": {"last_signal_at": _now(), "updated_at": _now()},
            },
        )
        return result.modified_count > 0

    async def set_project_next_steps(self, project_id: str, steps: list[str]) -> bool:
        oid = _safe_object_id(project_id)
        if oid is None:
            return False
        steps = [s.strip() for s in steps if s and s.strip()][:MAX_NEXT_STEPS]
        await self.projects.update_one(
            {"_id": oid},
            {"$set": {"next_steps": steps, "updated_at": _now()}},
        )
        return True

    # --------------------------------------------------- ambient/dedup helpers
    async def find_open_task_by_hash(self, content_hash: str) -> Optional[Task]:
        """Find an existing open task with the same normalized title."""
        doc = await self.tasks.find_one(
            {"content_hash": content_hash, "status": {"$in": list(OPEN_STATUSES)}}
        )
        return self._task_from_doc(doc) if doc else None

    async def fuzzy_find_project(self, hint: str) -> Optional[Project]:
        """Match a free-text hint to an existing project by exact slug or
        case-insensitive substring on name/slug. Returns at most one match
        (the most recently active one); returns None if no match — callers
        should NOT auto-create projects from hints in v1."""
        if not hint:
            return None
        slug_hint = _slugify(hint)
        # Exact slug first
        doc = await self.projects.find_one({"slug": slug_hint})
        if doc:
            return self._project_from_doc(doc)
        # Substring on name or slug
        regex = {"$regex": re.escape(hint.strip()), "$options": "i"}
        doc = await self.projects.find_one(
            {"$or": [{"name": regex}, {"slug": regex}]},
            sort=[("last_signal_at", -1), ("updated_at", -1)],
        )
        return self._project_from_doc(doc) if doc else None
