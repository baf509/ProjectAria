"""
ARIA - Claude Code Session Sensor

Purpose: Monitor ~/.claude/projects/ for Claude Code session activity.
Reads JSONL session files, extracts user messages and key topics,
and uses ClaudeRunner to produce session digests that ARIA can
reference in conversations.

This bridges the gap between Claude Code sessions and ARIA's memory,
giving ARIA awareness of what you've been working on in other contexts.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from aria.awareness.base import BaseSensor, Observation
from aria.config import settings

logger = logging.getLogger(__name__)

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"


class ClaudeSessionSensor(BaseSensor):
    """Watches Claude Code session files for new activity."""

    name = "claude_sessions"
    category = "claude"

    def __init__(
        self,
        max_session_age_hours: float = 48,
        max_messages_per_session: int = 50,
    ):
        self.max_age_hours = max_session_age_hours
        self.max_messages = max_messages_per_session
        # Track what we've already seen: session_file -> last_seen_byte_offset
        self._seen_offsets: dict[str, int] = {}
        # Track which sessions we've already produced a digest for
        self._digested_sessions: set[str] = set()

    def is_available(self) -> bool:
        return PROJECTS_DIR.is_dir()

    async def poll(self) -> list[Observation]:
        if not PROJECTS_DIR.is_dir():
            return []

        observations = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.max_age_hours)
        cutoff_ts = cutoff.timestamp()

        # Scan all project directories for JSONL session files
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue

            project_name = self._decode_project_name(project_dir.name)

            for jsonl_file in project_dir.glob("*.jsonl"):
                try:
                    # Skip files not modified recently
                    mtime = jsonl_file.stat().st_mtime
                    if mtime < cutoff_ts:
                        continue

                    obs = self._process_session_file(
                        jsonl_file, project_name, cutoff
                    )
                    observations.extend(obs)
                except Exception as e:
                    logger.debug("Error processing %s: %s", jsonl_file, e)

        return observations

    def _decode_project_name(self, encoded: str) -> str:
        """Decode project directory name: '-home-ben-Dev-Foo' -> '~/Dev/Foo'."""
        # Replace leading -home-<user> with ~
        parts = encoded.split("-")
        if len(parts) >= 3 and parts[1] == "home":
            # Skip empty first part, 'home', and username
            path_parts = parts[3:]
            return "~/" + "/".join(path_parts)
        return encoded

    def _process_session_file(
        self,
        filepath: Path,
        project_name: str,
        cutoff: datetime,
    ) -> list[Observation]:
        """Read new messages from a session JSONL file."""
        observations = []
        file_key = str(filepath)
        file_size = filepath.stat().st_size
        last_offset = self._seen_offsets.get(file_key, 0)

        # Skip if file hasn't grown
        if file_size <= last_offset:
            return observations

        # Read new content from where we left off
        new_user_messages = []
        new_assistant_summaries = []
        session_id = filepath.stem
        git_branch = None
        last_timestamp = None

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                f.seek(last_offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    entry_type = entry.get("type")
                    timestamp_str = entry.get("timestamp")

                    # Parse timestamp and skip old entries
                    if timestamp_str:
                        try:
                            ts = datetime.fromisoformat(
                                timestamp_str.replace("Z", "+00:00")
                            )
                            if ts < cutoff:
                                continue
                            last_timestamp = ts
                        except (ValueError, TypeError):
                            pass

                    if entry.get("gitBranch"):
                        git_branch = entry["gitBranch"]

                    if entry_type == "user":
                        content = self._extract_content(entry)
                        if content and len(content) > 5:
                            # Skip system/tool messages
                            if not content.startswith("<task-notification>"):
                                new_user_messages.append(content)

                    elif entry_type == "assistant":
                        content = self._extract_content(entry)
                        if content and len(content) > 20:
                            # Keep only text content, skip tool calls
                            new_assistant_summaries.append(content[:500])

                # Update offset to current file size
                self._seen_offsets[file_key] = f.tell()

        except Exception as e:
            logger.debug("Error reading session %s: %s", filepath.name, e)
            return observations

        if not new_user_messages:
            return observations

        # Produce an observation about the new activity
        # Summarize what the user was asking/doing
        msg_count = len(new_user_messages)
        sample_messages = new_user_messages[-5:]  # Last 5 user messages
        sample_text = " | ".join(
            msg[:100] for msg in sample_messages
        )

        branch_info = f" on {git_branch}" if git_branch else ""
        summary = (
            f"Claude Code session in {project_name}{branch_info}: "
            f"{msg_count} new message(s)"
        )

        # Build detail with the user messages for context
        detail_lines = [f"Project: {project_name}"]
        if git_branch:
            detail_lines.append(f"Branch: {git_branch}")
        detail_lines.append(f"Session: {session_id}")
        detail_lines.append(f"New messages: {msg_count}")
        detail_lines.append("")
        detail_lines.append("Recent user messages:")
        for msg in new_user_messages[-self.max_messages:]:
            detail_lines.append(f"  > {msg[:300]}")

        observations.append(Observation(
            sensor=self.name,
            category=self.category,
            event_type="session_activity",
            summary=summary,
            detail="\n".join(detail_lines)[:4000],
            severity="info",
            tags=[project_name, session_id[:8]],
            created_at=last_timestamp or datetime.now(timezone.utc),
        ))

        # If this session has significant activity and we haven't digested it,
        # flag it for ClaudeRunner analysis
        total_user_msgs = len(new_user_messages)
        if total_user_msgs >= 3 and session_id not in self._digested_sessions:
            observations.append(Observation(
                sensor=self.name,
                category=self.category,
                event_type="session_digest_needed",
                summary=f"Session in {project_name} has {total_user_msgs} messages — digest recommended",
                detail=self._build_digest_payload(
                    project_name, git_branch, session_id,
                    new_user_messages, new_assistant_summaries,
                ),
                severity="notice",
                tags=[project_name, "digest_needed"],
            ))
            self._digested_sessions.add(session_id)

        return observations

    def _extract_content(self, entry: dict) -> Optional[str]:
        """Extract text content from a JSONL message entry."""
        message = entry.get("message", {})
        content = message.get("content")

        if isinstance(content, str):
            return content.strip()

        if isinstance(content, list):
            # Content blocks — extract text blocks only
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        # Skip tool results
                        continue
                elif isinstance(block, str):
                    text_parts.append(block)
            return " ".join(text_parts).strip() if text_parts else None

        return None

    def _build_digest_payload(
        self,
        project_name: str,
        git_branch: Optional[str],
        session_id: str,
        user_messages: list[str],
        assistant_summaries: list[str],
    ) -> str:
        """Build the payload that will be sent to ClaudeRunner for digestion."""
        lines = [
            f"Project: {project_name}",
            f"Branch: {git_branch or 'unknown'}",
            f"Session: {session_id}",
            "",
            "Conversation flow:",
        ]

        # Interleave user/assistant messages (best effort)
        for i, msg in enumerate(user_messages[-20:]):
            lines.append(f"  USER: {msg[:400]}")
            if i < len(assistant_summaries):
                lines.append(f"  ASSISTANT: {assistant_summaries[i][:300]}")

        return "\n".join(lines)[:6000]
