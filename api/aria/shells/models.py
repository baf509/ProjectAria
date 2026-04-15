"""
ARIA - Watched Shells Models

Purpose: Pydantic models for shells, shell_events, and shell_snapshots.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


ShellStatus = Literal["active", "idle", "stopped", "unknown"]
ShellEventKind = Literal["output", "input", "system"]
ShellEventSource = Literal["pipe-pane", "send-keys", "hook", "reconciler"]


class Shell(BaseModel):
    name: str
    short_name: str
    project_dir: str = ""
    host: str = ""
    status: ShellStatus = "active"
    created_at: datetime
    last_activity_at: datetime
    last_output_at: Optional[datetime] = None
    last_input_at: Optional[datetime] = None
    line_count: int = 0
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ShellEvent(BaseModel):
    shell_name: str
    ts: datetime
    line_number: int
    kind: ShellEventKind
    text_raw: str
    text_clean: str
    source: ShellEventSource
    byte_offset: Optional[int] = None


class ShellSnapshot(BaseModel):
    shell_name: str
    ts: datetime
    content: str
    content_hash: str
    line_count_at_snapshot: int = 0


class ShellInput(BaseModel):
    text: str
    append_enter: bool = True
    literal: bool = False


class ShellTagsUpdate(BaseModel):
    tags: list[str]


class ShellListResponse(BaseModel):
    shells: list[Shell]


class ShellEventsResponse(BaseModel):
    events: list[ShellEvent]
    has_more: bool = False


class ShellInputResponse(BaseModel):
    ok: bool
    line_number: Optional[int] = None
