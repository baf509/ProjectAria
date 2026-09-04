"""
ARIA - Node API models

Pydantic models for the /api/v1/nodes surface: registration, event/snapshot
ingest from a remote node, and the pull-based command queue.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class NodeRegisterRequest(BaseModel):
    node_id: str
    hostname: str = ""
    os: str = ""
    arch: str = ""
    capabilities: list[str] = Field(default_factory=list)
    agent_version: str = ""


class NodeInfo(BaseModel):
    node_id: str
    hostname: str = ""
    os: str = ""
    arch: str = ""
    capabilities: list[str] = Field(default_factory=list)
    agent_version: str = ""
    status: str = "offline"  # online | offline (computed from last_heartbeat_at)
    registered_at: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None


class NodeHeartbeatRequest(BaseModel):
    # None means liveness-only. An explicit list is a complete authoritative
    # inventory captured by the node and is used for reconnect reconciliation.
    live_shells: Optional[list[str]] = None


class ShellEventIn(BaseModel):
    event_id: Optional[str] = None
    kind: str = "output"
    text_raw: str = ""
    text_clean: Optional[str] = None
    source: str = "pipe-pane"


class EventBatchIn(BaseModel):
    """A batch of captured lines for one shell, pushed by a node."""
    shell_name: str
    batch_id: Optional[str] = None
    project_dir: str = ""
    events: list[ShellEventIn] = Field(default_factory=list)
    stopped: bool = False  # node signals the shell's tmux session ended


class SnapshotIn(BaseModel):
    """A pane rehydration snapshot for one shell, pushed by a node."""
    shell_name: str
    content: str


class CommandOut(BaseModel):
    """A queued command handed to the node to execute against its local tmux."""
    id: str
    kind: str  # send_input | start_session | stop
    args: dict[str, Any] = Field(default_factory=dict)


class CommandResultIn(BaseModel):
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
