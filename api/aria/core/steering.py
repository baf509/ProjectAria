"""
ARIA - Steering Messages

Purpose: Allow users to inject mid-execution messages that steer the orchestrator
between tool calls or LLM rounds. Messages are queued per-conversation and
consumed by the orchestrator at safe checkpoints.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SteeringMessage:
    """A mid-execution steering message from the user."""
    content: str
    priority: str = "normal"  # "normal" | "interrupt"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SteeringQueue:
    """Per-conversation message queue for mid-execution steering.

    The orchestrator checks this queue between tool calls and LLM rounds.
    'interrupt' priority messages cause the current round to stop and
    re-prompt with the steering content.
    """

    def __init__(self):
        self._queues: dict[str, asyncio.Queue[SteeringMessage]] = defaultdict(
            lambda: asyncio.Queue(maxsize=10)
        )

    def enqueue(self, conversation_id: str, content: str, priority: str = "normal") -> bool:
        """Queue a steering message for a conversation.

        Returns False if the queue is full.
        """
        q = self._queues[conversation_id]
        msg = SteeringMessage(content=content, priority=priority)
        try:
            q.put_nowait(msg)
            logger.info(
                "Steering message queued for %s (priority=%s): %.60s...",
                conversation_id, priority, content,
            )
            return True
        except asyncio.QueueFull:
            logger.warning("Steering queue full for %s, dropping message", conversation_id)
            return False

    def drain(self, conversation_id: str) -> list[SteeringMessage]:
        """Drain all pending steering messages for a conversation.

        Returns them in insertion order, interrupts first.
        """
        q = self._queues.get(conversation_id)
        if q is None or q.empty():
            return []

        messages = []
        while not q.empty():
            try:
                messages.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break

        # Sort: interrupts first, then by creation time
        messages.sort(key=lambda m: (0 if m.priority == "interrupt" else 1, m.created_at))
        return messages

    def has_interrupt(self, conversation_id: str) -> bool:
        """Check if there's an interrupt-priority message without consuming it."""
        q = self._queues.get(conversation_id)
        if q is None or q.empty():
            return False
        # Peek: drain and re-queue (asyncio.Queue has no peek)
        items = []
        has = False
        while not q.empty():
            try:
                item = q.get_nowait()
                items.append(item)
                if item.priority == "interrupt":
                    has = True
            except asyncio.QueueEmpty:
                break
        for item in items:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                break
        return has

    def clear(self, conversation_id: str) -> int:
        """Clear all pending messages for a conversation. Returns count cleared."""
        q = self._queues.pop(conversation_id, None)
        if q is None:
            return 0
        count = q.qsize()
        return count


# Module-level singleton
steering_queue = SteeringQueue()
