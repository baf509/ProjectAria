"""
ARIA - Inter-Agent Messaging Protocol

Purpose: Structured agent-to-agent communication for task completion,
handoffs, and result passing. Inspired by Gas Town's mail protocol.

Message types:
- TASK_DONE:     Agent completed its task, includes result summary
- HANDOFF:       Agent passes work to another agent with context
- RESULT:        Agent delivers structured output to the orchestrator
- ERROR:         Agent encountered a fatal error
- CHECKPOINT:    Agent saved a checkpoint (informational)

Messages are stored in MongoDB and can be queried by recipient,
conversation, or type. The orchestrator polls for messages addressed
to it and processes them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


class MessageType(str, Enum):
    TASK_DONE = "task_done"
    HANDOFF = "handoff"
    RESULT = "result"
    ERROR = "error"
    CHECKPOINT = "checkpoint"


class AgentMessage:
    """A structured message between agents."""

    def __init__(
        self,
        sender: str,
        recipient: str,
        msg_type: MessageType,
        subject: str,
        body: Optional[str] = None,
        metadata: Optional[dict] = None,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        message_id: Optional[str] = None,
        created_at: Optional[datetime] = None,
        read: bool = False,
    ):
        self.message_id = message_id or str(uuid4())
        self.sender = sender
        self.recipient = recipient
        self.msg_type = msg_type
        self.subject = subject
        self.body = body
        self.metadata = metadata or {}
        self.conversation_id = conversation_id
        self.session_id = session_id
        self.created_at = created_at or datetime.now(timezone.utc)
        self.read = read

    def to_dict(self) -> dict:
        return {
            "_id": self.message_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "type": self.msg_type.value,
            "subject": self.subject,
            "body": self.body,
            "metadata": self.metadata,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "read": self.read,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentMessage:
        return cls(
            message_id=data.get("_id") or data.get("message_id"),
            sender=data["sender"],
            recipient=data["recipient"],
            msg_type=MessageType(data["type"]),
            subject=data["subject"],
            body=data.get("body"),
            metadata=data.get("metadata", {}),
            conversation_id=data.get("conversation_id"),
            session_id=data.get("session_id"),
            created_at=data.get("created_at"),
            read=data.get("read", False),
        )


class AgentMailbox:
    """MongoDB-backed inter-agent messaging system."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db

    async def send(self, message: AgentMessage) -> str:
        """Send a message. Returns the message ID."""
        await self.db.agent_mail.insert_one(message.to_dict())
        logger.info(
            "Mail: %s -> %s [%s] %s",
            message.sender, message.recipient,
            message.msg_type.value, message.subject,
        )
        return message.message_id

    async def send_task_done(
        self,
        sender: str,
        recipient: str,
        session_id: str,
        result_summary: str,
        exit_status: str = "completed",
        conversation_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        """Convenience: send a TASK_DONE message."""
        msg = AgentMessage(
            sender=sender,
            recipient=recipient,
            msg_type=MessageType.TASK_DONE,
            subject=f"TASK_DONE {sender}",
            body=result_summary,
            metadata={"exit_status": exit_status, **(metadata or {})},
            conversation_id=conversation_id,
            session_id=session_id,
        )
        return await self.send(msg)

    async def send_handoff(
        self,
        sender: str,
        recipient: str,
        task_description: str,
        context: str,
        session_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        """Convenience: send a HANDOFF message — pass work to another agent."""
        msg = AgentMessage(
            sender=sender,
            recipient=recipient,
            msg_type=MessageType.HANDOFF,
            subject=f"HANDOFF from {sender}",
            body=f"## Task\n\n{task_description}\n\n## Context\n\n{context}",
            metadata=metadata or {},
            conversation_id=conversation_id,
            session_id=session_id,
        )
        return await self.send(msg)

    async def send_error(
        self,
        sender: str,
        recipient: str,
        error: str,
        session_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> str:
        """Convenience: send an ERROR message."""
        msg = AgentMessage(
            sender=sender,
            recipient=recipient,
            msg_type=MessageType.ERROR,
            subject=f"ERROR {sender}",
            body=error,
            conversation_id=conversation_id,
            session_id=session_id,
        )
        return await self.send(msg)

    async def get_unread(
        self,
        recipient: str,
        msg_type: Optional[MessageType] = None,
        limit: int = 50,
    ) -> list[AgentMessage]:
        """Get unread messages for a recipient."""
        query: dict = {"recipient": recipient, "read": False}
        if msg_type:
            query["type"] = msg_type.value
        docs = await self.db.agent_mail.find(query).sort(
            "created_at", 1
        ).to_list(length=limit)
        return [AgentMessage.from_dict(d) for d in docs]

    async def mark_read(self, message_id: str) -> None:
        """Mark a message as read."""
        await self.db.agent_mail.update_one(
            {"_id": message_id},
            {"$set": {"read": True, "read_at": datetime.now(timezone.utc)}},
        )

    async def mark_all_read(self, recipient: str) -> int:
        """Mark all messages for a recipient as read."""
        result = await self.db.agent_mail.update_many(
            {"recipient": recipient, "read": False},
            {"$set": {"read": True, "read_at": datetime.now(timezone.utc)}},
        )
        return result.modified_count

    async def get_conversation_mail(
        self,
        conversation_id: str,
        limit: int = 50,
    ) -> list[AgentMessage]:
        """Get all messages related to a conversation."""
        docs = await self.db.agent_mail.find(
            {"conversation_id": conversation_id}
        ).sort("created_at", 1).to_list(length=limit)
        return [AgentMessage.from_dict(d) for d in docs]

    async def get_session_mail(
        self,
        session_id: str,
    ) -> list[AgentMessage]:
        """Get all messages related to a coding session."""
        docs = await self.db.agent_mail.find(
            {"session_id": session_id}
        ).sort("created_at", 1).to_list(length=50)
        return [AgentMessage.from_dict(d) for d in docs]
