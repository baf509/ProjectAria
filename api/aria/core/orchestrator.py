"""
ARIA - Agent Orchestrator

Phase: 1
Purpose: Main agent loop - processes messages and streams responses

Related Spec Sections:
- Section 2.2: Request Flow
- Section 8: Phase 1 Implementation
"""

import uuid
from datetime import datetime
from typing import AsyncIterator
from bson import ObjectId

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.llm.manager import llm_manager
from aria.llm.base import Message, StreamChunk


class Orchestrator:
    """Main agent orchestration logic."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db

    async def process_message(
        self, conversation_id: str, user_message: str
    ) -> AsyncIterator[StreamChunk]:
        """
        Process a user message and stream the response.

        Args:
            conversation_id: ID of the conversation
            user_message: User's message content

        Yields:
            StreamChunk objects for streaming response
        """
        # 1. Load conversation
        conversation = await self.db.conversations.find_one(
            {"_id": ObjectId(conversation_id)}
        )
        if not conversation:
            yield StreamChunk(type="error", error="Conversation not found")
            return

        # 2. Load agent
        agent = await self.db.agents.find_one(
            {"_id": ObjectId(conversation["agent_id"])}
        )
        if not agent:
            yield StreamChunk(type="error", error="Agent not found")
            return

        # 3. Build message list (system prompt + conversation history + new message)
        messages = []

        # Add system prompt
        messages.append(Message(role="system", content=agent["system_prompt"]))

        # Add conversation history (last N messages)
        for msg in conversation.get("messages", [])[-20:]:
            messages.append(Message(role=msg["role"], content=msg["content"]))

        # Add new user message
        messages.append(Message(role="user", content=user_message))

        # 4. Save user message to conversation
        user_msg_doc = {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": user_message,
            "created_at": datetime.utcnow(),
            "memory_processed": False,
        }
        await self.db.conversations.update_one(
            {"_id": ObjectId(conversation_id)},
            {
                "$push": {"messages": user_msg_doc},
                "$set": {"updated_at": datetime.utcnow()},
                "$inc": {"stats.message_count": 1},
            },
        )

        # 5. Get LLM adapter
        llm_config = agent["llm"]
        adapter = llm_manager.get_adapter(llm_config["backend"], llm_config["model"])

        # 6. Stream response
        assistant_content_parts = []
        usage = {}

        try:
            async for chunk in adapter.stream(
                messages,
                temperature=llm_config.get("temperature", 0.7),
                max_tokens=llm_config.get("max_tokens", 4096),
            ):
                if chunk.type == "text":
                    assistant_content_parts.append(chunk.content)
                    yield chunk
                elif chunk.type == "done":
                    usage = chunk.usage
                    yield chunk
                elif chunk.type == "error":
                    yield chunk
                    return

        except Exception as e:
            yield StreamChunk(type="error", error=f"LLM error: {str(e)}")
            return

        # 7. Save assistant response
        assistant_content = "".join(assistant_content_parts)
        assistant_msg_doc = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": assistant_content,
            "model": llm_config["model"],
            "tokens": usage,
            "created_at": datetime.utcnow(),
            "memory_processed": False,
        }
        await self.db.conversations.update_one(
            {"_id": ObjectId(conversation_id)},
            {
                "$push": {"messages": assistant_msg_doc},
                "$set": {"updated_at": datetime.utcnow()},
                "$inc": {
                    "stats.message_count": 1,
                    "stats.total_tokens": usage.get("input_tokens", 0)
                    + usage.get("output_tokens", 0),
                },
            },
        )
