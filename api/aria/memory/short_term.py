"""
ARIA - Short-Term Memory

Phase: 2
Purpose: Fast retrieval of recent/current context

Related Spec Sections:
- Section 3.2: Short-Term Memory Implementation
"""

from datetime import datetime, timedelta
from typing import Optional
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase


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

        # TODO: Trim to fit token budget if needed
        # For Phase 2, we'll just use max_messages
        return messages

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
        cutoff = datetime.utcnow() - timedelta(hours=hours)

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

    def _trim_to_tokens(self, messages: list[dict], max_tokens: int) -> list[dict]:
        """
        Trim messages to fit token budget.
        Simple approximation: 1 token ≈ 4 characters.

        Args:
            messages: List of messages
            max_tokens: Maximum tokens allowed

        Returns:
            Trimmed list of messages
        """
        # Simple heuristic: 1 token ≈ 4 chars
        chars_per_token = 4
        max_chars = max_tokens * chars_per_token

        total_chars = 0
        trimmed = []

        # Keep messages from most recent backwards
        for msg in reversed(messages):
            content = msg.get("content", "")
            msg_chars = len(content)

            if total_chars + msg_chars > max_chars:
                break

            trimmed.insert(0, msg)
            total_chars += msg_chars

        return trimmed
