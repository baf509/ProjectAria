"""
ARIA - Short-Term Memory

Phase: 2
Purpose: Fast retrieval of recent/current context

Related Spec Sections:
- Section 3.2: Short-Term Memory Implementation
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.core.tokenizer import count_tokens


class ConversationSummary:
    """Summary of a conversation for context."""

    def __init__(self, id: str, title: str, summary: Optional[str], updated_at: datetime):
        self.id = id
        self.title = title
        self.summary = summary
        self.updated_at = updated_at

    @classmethod
    def from_doc(cls, doc: dict):
        """Create from MongoDB document."""
        return cls(
            id=str(doc["_id"]),
            title=doc["title"],
            summary=doc.get("summary"),
            updated_at=doc["updated_at"],
        )


class ShortTermMemory:
    """
    Fast retrieval from recent context. No embeddings needed.
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db

    async def get_current_conversation_context(
        self,
        conversation_id: str,
        max_messages: int = 20,
        max_tokens: int = 8000,
        model: str = "default",
    ) -> list[dict]:
        """
        Get recent messages from current conversation.
        Simple MongoDB query, very fast.

        Args:
            conversation_id: Conversation ID
            max_messages: Maximum number of messages to retrieve
            max_tokens: Maximum tokens to include (approximate)

        Returns:
            List of message dictionaries
        """
        conversation = await self.db.conversations.find_one(
            {"_id": ObjectId(conversation_id)},
            {"messages": {"$slice": -max_messages}},
        )

        if not conversation:
            return []

        messages = conversation.get("messages", [])

        return self._trim_to_tokens(messages, max_tokens, model)

    async def get_recent_conversations_context(
        self, hours: int = 24, limit: int = 5
    ) -> list[ConversationSummary]:
        """
        Get summaries of recent conversations for context.
        Useful for "what were we discussing yesterday?"

        Args:
            hours: Look back this many hours
            limit: Maximum number of conversations to return

        Returns:
            List of conversation summaries
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        conversations = (
            await self.db.conversations.find(
                {"updated_at": {"$gte": cutoff}},
                {"title": 1, "summary": 1, "updated_at": 1},
            )
            .sort("updated_at", -1)
            .limit(limit)
            .to_list(length=limit)
        )

        return [ConversationSummary.from_doc(c) for c in conversations]

    async def archive_old_conversations(
        self, days: int = 90, batch_size: int = 100
    ) -> int:
        """
        Archive conversations older than `days` by setting status to 'archived'.
        Keeps the database performant by reducing active conversation count.

        Args:
            days: Archive conversations not updated in this many days
            batch_size: Maximum conversations to archive per call

        Returns:
            Number of conversations archived
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        result = await self.db.conversations.update_many(
            {
                "status": "active",
                "updated_at": {"$lt": cutoff},
            },
            {
                "$set": {
                    "status": "archived",
                    "archived_at": datetime.now(timezone.utc),
                }
            },
        )

        if result.modified_count > 0:
            import logging
            logging.getLogger(__name__).info(
                "Archived %d conversations older than %d days",
                result.modified_count, days,
            )

        return result.modified_count

    def _trim_to_tokens(
        self,
        messages: list[dict],
        max_tokens: int,
        model: str,
    ) -> list[dict]:
        """
        Trim messages to fit token budget.

        Args:
            messages: List of messages
            max_tokens: Maximum tokens allowed
            model: Model name used for token counting

        Returns:
            Trimmed list of messages
        """
        total_tokens = 0
        trimmed = []

        # Keep messages from most recent backwards
        for msg in reversed(messages):
            content = msg.get("content", "")
            msg_tokens = count_tokens(content, model) + 4

            if trimmed and total_tokens + msg_tokens > max_tokens:
                break
            if not trimmed and msg_tokens > max_tokens:
                return [msg]

            trimmed.insert(0, msg)
            total_tokens += msg_tokens

        return trimmed
