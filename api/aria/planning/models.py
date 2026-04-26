"""
ARIA - Planning Models (tasks + projects)

Pydantic shapes for the to-do list and long-term project tracker. Tasks have
a lifecycle (proposed -> active -> done | dismissed); projects are coarse
groupings the user works on with a rolling activity log.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field


TaskStatus = Literal["proposed", "active", "done", "dismissed"]
ProjectStatus = Literal["active", "paused", "archived"]
TaskSourceType = Literal["manual", "conversation", "shell", "awareness", "import"]


class TaskSource(BaseModel):
    """Where a task came from. `type=manual` for user-created; everything else
    is ambient extraction."""
    type: TaskSourceType
    conversation_id: Optional[str] = None
    shell_name: Optional[str] = None
    message_ids: Optional[list[str]] = None
    extracted_at: Optional[datetime] = None
    confidence: Optional[float] = None


class Task(BaseModel):
    """A to-do item. Persisted in the `tasks` collection."""
    id: str
    title: str
    notes: Optional[str] = None
    status: TaskStatus = "active"
    due_at: Optional[datetime] = None
    project_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    source: TaskSource
    content_hash: str  # sha256 of normalized title; used for hash-based dedup
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


class TaskCreateRequest(BaseModel):
    """Manual task creation."""
    title: str = Field(..., min_length=1, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=4000)
    due_at: Optional[datetime] = None
    project_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    status: TaskStatus = "active"


class TaskUpdateRequest(BaseModel):
    """Partial update for a task."""
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=4000)
    status: Optional[TaskStatus] = None
    due_at: Optional[datetime] = None
    project_id: Optional[str] = None
    tags: Optional[list[str]] = None


class TaskListResponse(BaseModel):
    tasks: list[Task]


class ProjectActivity(BaseModel):
    """One entry in a project's recent_activity log (capped, FIFO-evicted)."""
    at: datetime
    source: str  # e.g. "conversation:<id>", "manual", "shell:<name>"
    note: str


class Project(BaseModel):
    """A long-running effort the user is working on."""
    id: str
    name: str
    slug: str
    summary: str = ""
    status: ProjectStatus = "active"
    next_steps: list[str] = Field(default_factory=list)  # rolling, max ~5
    relevant_paths: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    recent_activity: list[ProjectActivity] = Field(default_factory=list)  # capped at 20
    created_at: datetime
    updated_at: datetime
    last_signal_at: Optional[datetime] = None


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    slug: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="URL-safe identifier; auto-derived from name if omitted",
    )
    summary: str = Field(default="", max_length=2000)
    status: ProjectStatus = "active"
    next_steps: list[str] = Field(default_factory=list)
    relevant_paths: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    summary: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[ProjectStatus] = None
    next_steps: Optional[list[str]] = None
    relevant_paths: Optional[list[str]] = None
    tags: Optional[list[str]] = None


class ProjectListResponse(BaseModel):
    projects: list[Project]
