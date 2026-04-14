"""
ARIA - Session Checkpointing for Crash Recovery

Purpose: Write checkpoint state for coding sessions so crashed agents
can be resumed with context. Inspired by Gas Town's checkpoint system.

Checkpoints capture: current task, modified files, branch, last commit,
and freeform notes. Stored in MongoDB alongside the session document.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


class SessionCheckpoint:
    """Checkpoint state for a coding session."""

    def __init__(
        self,
        session_id: str,
        workspace: str,
        branch: Optional[str] = None,
        last_commit: Optional[str] = None,
        modified_files: Optional[list[str]] = None,
        current_step: Optional[str] = None,
        notes: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ):
        self.session_id = session_id
        self.workspace = workspace
        self.branch = branch
        self.last_commit = last_commit
        self.modified_files = modified_files or []
        self.current_step = current_step
        self.notes = notes
        self.timestamp = timestamp or datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "branch": self.branch,
            "last_commit": self.last_commit,
            "modified_files": self.modified_files,
            "current_step": self.current_step,
            "notes": self.notes,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SessionCheckpoint:
        return cls(
            session_id=data["session_id"],
            workspace=data.get("workspace", ""),
            branch=data.get("branch"),
            last_commit=data.get("last_commit"),
            modified_files=data.get("modified_files", []),
            current_step=data.get("current_step"),
            notes=data.get("notes"),
            timestamp=data.get("timestamp"),
        )


async def _get_git_state(workspace: str) -> dict:
    """Capture current git state from a workspace directory."""
    state = {"branch": None, "last_commit": None, "modified_files": []}

    async def _run_git(*args: str) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", workspace, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None
        return stdout.decode().strip() if proc.returncode == 0 else None

    try:
        branch = await _run_git("rev-parse", "--abbrev-ref", "HEAD")
        if branch is not None:
            state["branch"] = branch
    except Exception:
        pass

    try:
        commit = await _run_git("rev-parse", "--short", "HEAD")
        if commit is not None:
            state["last_commit"] = commit
    except Exception:
        pass

    try:
        diff_output = await _run_git("diff", "--name-only")
        if diff_output is not None:
            files = [f for f in diff_output.split("\n") if f]
            state["modified_files"] = files
    except Exception:
        pass

    return state


async def write_checkpoint(
    db: AsyncIOMotorDatabase,
    session_id: str,
    workspace: str,
    current_step: Optional[str] = None,
    notes: Optional[str] = None,
) -> SessionCheckpoint:
    """Write a checkpoint for a coding session, capturing git state."""
    git_state = await _get_git_state(workspace)

    checkpoint = SessionCheckpoint(
        session_id=session_id,
        workspace=workspace,
        branch=git_state["branch"],
        last_commit=git_state["last_commit"],
        modified_files=git_state["modified_files"],
        current_step=current_step,
        notes=notes,
    )

    await db.session_checkpoints.update_one(
        {"session_id": session_id},
        {"$set": checkpoint.to_dict()},
        upsert=True,
    )

    logger.info(
        "Checkpoint written for session %s (branch=%s, commit=%s, files=%d)",
        session_id, checkpoint.branch, checkpoint.last_commit,
        len(checkpoint.modified_files),
    )
    return checkpoint


async def read_checkpoint(
    db: AsyncIOMotorDatabase,
    session_id: str,
) -> Optional[SessionCheckpoint]:
    """Read the latest checkpoint for a session."""
    doc = await db.session_checkpoints.find_one({"session_id": session_id})
    if doc is None:
        return None
    return SessionCheckpoint.from_dict(doc)


async def find_resumable_checkpoint(
    db: AsyncIOMotorDatabase,
    workspace: str,
) -> Optional[SessionCheckpoint]:
    """Find the most recent checkpoint for a workspace from a failed/crashed session."""
    doc = await db.session_checkpoints.find_one(
        {"workspace": workspace},
        sort=[("timestamp", -1)],
    )
    if doc is None:
        return None
    return SessionCheckpoint.from_dict(doc)


def build_resume_prompt(checkpoint: SessionCheckpoint, original_prompt: str) -> str:
    """Build a resume prompt that includes checkpoint context."""
    parts = [
        "You are resuming a previous coding session that was interrupted.",
        "",
        f"**Original task:** {original_prompt}",
        "",
        "## Previous Session State",
    ]

    if checkpoint.branch:
        parts.append(f"- **Branch:** {checkpoint.branch}")
    if checkpoint.last_commit:
        parts.append(f"- **Last commit:** {checkpoint.last_commit}")
    if checkpoint.current_step:
        parts.append(f"- **Was working on:** {checkpoint.current_step}")
    if checkpoint.modified_files:
        parts.append(f"- **Modified files:** {', '.join(checkpoint.modified_files[:20])}")
    if checkpoint.notes:
        parts.append(f"- **Notes:** {checkpoint.notes}")

    parts.extend([
        "",
        "## Instructions",
        "",
        "1. Check the current state of the workspace (git status, modified files)",
        "2. Determine what was completed and what still needs to be done",
        "3. Continue the original task from where it left off",
        "4. Do not redo work that was already completed",
    ])

    return "\n".join(parts)
