"""
ARIA - Dream Context Collector

Purpose: Gather context from MongoDB for dream reflection.
Collects recent memories, conversation summaries, previous journal
entries, and the current soul file — everything ARIA needs to reflect.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.core.soul import soul_manager

logger = logging.getLogger(__name__)


class DreamCollector:
    """Gather context for a dream cycle from MongoDB."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db

    async def collect(self) -> dict:
        """
        Collect all context needed for a dream reflection.

        Returns:
            Dict with keys: soul, memories, conversations, journal
        """
        soul = soul_manager.read() or "(No soul file found)"
        memories = await self._recent_memories()
        conversations = await self._recent_conversations()
        journal = await self._recent_journal_entries()

        return {
            "soul": soul,
            "memories": memories,
            "conversations": conversations,
            "journal": journal,
        }

    async def _recent_memories(self) -> str:
        """Get recent and important memories as formatted text."""
        max_memories = settings.dream_max_memories
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)

        # Mix recent memories with most-accessed ones
        recent = await self.db.memories.find(
            {"status": "active", "created_at": {"$gte": cutoff}},
            {"content": 1, "content_type": 1, "categories": 1,
             "importance": 1, "created_at": 1, "access_count": 1},
        ).sort("created_at", -1).limit(max_memories // 2).to_list(
            length=max_memories // 2
        )

        popular = await self.db.memories.find(
            {"status": "active"},
            {"content": 1, "content_type": 1, "categories": 1,
             "importance": 1, "created_at": 1, "access_count": 1},
        ).sort("access_count", -1).limit(max_memories // 2).to_list(
            length=max_memories // 2
        )

        # Deduplicate by _id
        seen = set()
        all_memories = []
        for mem in recent + popular:
            mid = str(mem["_id"])
            if mid not in seen:
                seen.add(mid)
                all_memories.append(mem)

        if not all_memories:
            return "(No memories stored yet)"

        lines = []
        for mem in all_memories:
            mid = str(mem["_id"])
            ctype = mem.get("content_type", "unknown")
            cats = ", ".join(mem.get("categories", []))
            imp = mem.get("importance", 0.5)
            access = mem.get("access_count", 0)
            date = mem.get("created_at", "unknown")
            if hasattr(date, "strftime"):
                date = date.strftime("%Y-%m-%d")
            content = mem.get("content", "")
            lines.append(
                f"- [ID:{mid}] [{ctype}] (importance:{imp}, accessed:{access}x, date:{date}) "
                f"{content}"
            )
            if cats:
                lines[-1] += f" [tags: {cats}]"

        return "\n".join(lines)

    async def _recent_conversations(self) -> str:
        """Get recent conversation summaries."""
        max_convos = settings.dream_max_conversations
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)

        conversations = await self.db.conversations.find(
            {"updated_at": {"$gte": cutoff}, "status": "active"},
            {"title": 1, "summary": 1, "updated_at": 1,
             "stats": 1, "messages": {"$slice": -3}},
        ).sort("updated_at", -1).limit(max_convos).to_list(length=max_convos)

        if not conversations:
            return "(No recent conversations)"

        lines = []
        for conv in conversations:
            title = conv.get("title", "Untitled")
            date = conv.get("updated_at", "unknown")
            if hasattr(date, "strftime"):
                date = date.strftime("%Y-%m-%d %H:%M")
            stats = conv.get("stats", {})
            msg_count = stats.get("message_count", 0)
            summary = conv.get("summary")

            lines.append(f"### {title} ({date}, {msg_count} messages)")
            if summary:
                lines.append(f"Summary: {summary}")
            else:
                # Fall back to last few messages
                messages = conv.get("messages", [])
                for msg in messages:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")[:200]
                    lines.append(f"  {role}: {content}")
            lines.append("")

        return "\n".join(lines)

    async def _recent_journal_entries(self) -> str:
        """Get previous dream journal entries for continuity."""
        max_entries = settings.dream_max_journal_entries

        entries = await self.db.dream_journal.find(
            {},
            {"journal_entry": 1, "created_at": 1,
             "connections": 1, "knowledge_gaps": 1},
        ).sort("created_at", -1).limit(max_entries).to_list(length=max_entries)

        if not entries:
            return "(This is your first dream cycle — you have no previous journal entries)"

        lines = []
        for entry in entries:
            date = entry.get("created_at", "unknown")
            if hasattr(date, "strftime"):
                date = date.strftime("%Y-%m-%d %H:%M")
            lines.append(f"### {date}")
            lines.append(entry.get("journal_entry", "(empty)"))

            connections = entry.get("connections", [])
            if connections:
                lines.append(f"Connections found: {len(connections)}")
                for conn in connections[:3]:
                    between = conn.get("between", [])
                    insight = conn.get("insight", "")
                    lines.append(f"  - {' ↔ '.join(between)}: {insight}")

            gaps = entry.get("knowledge_gaps", [])
            if gaps:
                lines.append(f"Knowledge gaps: {', '.join(gaps[:3])}")

            lines.append("")

        return "\n".join(lines)
