"""
ARIA - Watched Shells Models

Purpose: Pydantic models for shells, shell_events, and shell_snapshots.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


ShellStatus = Literal["active", "idle", "stopped", "unknown"]
ShellEventKind = Literal["output", "input", "system"]
ShellEventSource = Literal["pipe-pane", "send-keys", "hook", "reconciler", "backfill"]


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

    @model_validator(mode="before")
    @classmethod
    def _ensure_output_newline(cls, data: Any) -> Any:
        """Backwards-compat shim for events captured before the strip-newline
        fix. Older capture.py rstripped '\\n' from text_raw; new capture
        preserves it. Re-append a single LF for output events that lack one
        so legacy data renders without running together as a single line.
        New events (already terminated) are unaffected.
        """
        if not isinstance(data, dict):
            return data
        if data.get("kind") == "output":
            raw = data.get("text_raw")
            if isinstance(raw, str) and raw and not raw.endswith(("\n", "\r")):
                data = {**data, "text_raw": raw + "\n"}
        return data


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


class ShellCreateRequest(BaseModel):
    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$",
    )
    workdir: Optional[str] = None
    launch_claude: bool = True
    cols: Optional[int] = Field(default=None, ge=20, le=500)
    rows: Optional[int] = Field(default=None, ge=10, le=200)


class ShellResizeRequest(BaseModel):
    cols: int = Field(..., ge=20, le=500)
    rows: int = Field(..., ge=10, le=200)


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
