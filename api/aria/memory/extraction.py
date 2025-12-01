"""
ARIA - Memory Extraction

Phase: 2
Purpose: Extract memories from conversations using LLM

Related Spec Sections:
- Section 3: Memory Architecture
"""

import json
from datetime import datetime
from typing import Optional
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.llm.manager import llm_manager
from aria.llm.base import Message
from aria.memory.long_term import LongTermMemory


EXTRACTION_PROMPT = """You are a memory extraction assistant. Your job is to analyze conversation messages and extract important facts, preferences, and information that should be remembered long-term.

Review the following conversation messages and extract any memories worth saving. Focus on:
- User preferences and likes/dislikes
- Important facts about the user
- Significant decisions or plans
- Skills or expertise mentioned
- Important context that would be useful in future conversations

For each memory, provide:
1. content: The memory text (concise but complete)
2. content_type: One of: fact, preference, event, skill, document
3. categories: List of relevant categories/tags
4. importance: Score from 0.0 to 1.0

Return your response as a JSON array of memory objects. If no significant memories are found, return an empty array.

Example output:
[
  {{
    "content": "User prefers Python over JavaScript for backend development",
    "content_type": "preference",
    "categories": ["coding", "preferences"],
    "importance": 0.7
  }},
  {{
    "content": "User is working on an AI agent platform called ARIA",
    "content_type": "fact",
    "categories": ["projects", "work"],
    "importance": 0.9
  }}
]

Conversation messages:
{messages}

Extract memories (return JSON array only):"""


class MemoryExtractor:
    """
    Extracts memories from conversations using LLM.
    Runs as background task after conversations.
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.long_term_memory = LongTermMemory(db)

    async def extract_from_conversation(
        self,
        conversation_id: str,
        batch_size: int = 10,
        llm_backend: str = "ollama",
        llm_model: str = "llama3.2:latest",
    ) -> int:
        """
        Extract memories from unprocessed messages in a conversation.

        Args:
            conversation_id: Conversation ID
            batch_size: Number of messages to process at once
            llm_backend: LLM backend to use for extraction
            llm_model: LLM model to use

        Returns:
            Number of memories extracted
        """
        # Get conversation
        conversation = await self.db.conversations.find_one(
            {"_id": ObjectId(conversation_id)}
        )

        if not conversation:
            return 0

        # Find unprocessed messages
        unprocessed = [
            msg
            for msg in conversation.get("messages", [])
            if not msg.get("memory_processed", False)
        ]

        if not unprocessed:
            return 0

        # Process in batches
        total_extracted = 0

        for i in range(0, len(unprocessed), batch_size):
            batch = unprocessed[i : i + batch_size]
            extracted = await self._extract_batch(
                conversation_id, batch, llm_backend, llm_model
            )
            total_extracted += extracted

        return total_extracted

    async def _extract_batch(
        self,
        conversation_id: str,
        messages: list[dict],
        llm_backend: str,
        llm_model: str,
    ) -> int:
        """
        Extract memories from a batch of messages.

        Args:
            conversation_id: Conversation ID
            messages: List of message dicts
            llm_backend: LLM backend
            llm_model: LLM model

        Returns:
            Number of memories extracted
        """
        # Format messages for extraction prompt
        messages_text = "\n\n".join(
            [f"{msg['role'].upper()}: {msg['content']}" for msg in messages]
        )

        prompt = EXTRACTION_PROMPT.format(messages=messages_text)

        # Get LLM adapter
        adapter = llm_manager.get_adapter(llm_backend, llm_model)

        # Extract memories
        try:
            response, _, _ = await adapter.complete(
                messages=[Message(role="user", content=prompt)],
                temperature=0.3,  # Lower temperature for more consistent extraction
                max_tokens=2048,
            )

            # Parse JSON response
            memories = json.loads(response.strip())

            if not isinstance(memories, list):
                print(f"Unexpected extraction response format: {response}")
                return 0

            # Create memories
            extracted_count = 0
            for memory_data in memories:
                try:
                    memory_id = await self.long_term_memory.create_memory(
                        content=memory_data["content"],
                        content_type=memory_data.get("content_type", "fact"),
                        categories=memory_data.get("categories", []),
                        importance=memory_data.get("importance", 0.5),
                        confidence=0.8,  # Auto-extracted, moderate confidence
                        source={
                            "type": "conversation",
                            "conversation_id": ObjectId(conversation_id),
                            "message_ids": [msg["id"] for msg in messages],
                            "extracted_at": datetime.utcnow(),
                        },
                    )
                    extracted_count += 1
                except Exception as e:
                    print(f"Error creating memory: {e}")
                    continue

            # Mark messages as processed
            message_ids = [msg["id"] for msg in messages]
            await self.db.conversations.update_one(
                {"_id": ObjectId(conversation_id)},
                {
                    "$set": {
                        "messages.$[elem].memory_processed": True
                        for elem in message_ids
                    }
                },
                array_filters=[{"elem.id": {"$in": message_ids}}],
            )

            return extracted_count

        except json.JSONDecodeError as e:
            print(f"Failed to parse extraction response: {e}")
            print(f"Response was: {response}")
            return 0
        except Exception as e:
            print(f"Memory extraction error: {e}")
            return 0

    async def extract_from_text(
        self,
        text: str,
        llm_backend: str = "ollama",
        llm_model: str = "llama3.2:latest",
    ) -> list[dict]:
        """
        Extract memories from arbitrary text.

        Args:
            text: Text to extract from
            llm_backend: LLM backend
            llm_model: LLM model

        Returns:
            List of extracted memory data
        """
        prompt = EXTRACTION_PROMPT.format(messages=f"USER: {text}")

        adapter = llm_manager.get_adapter(llm_backend, llm_model)

        try:
            response, _, _ = await adapter.complete(
                messages=[Message(role="user", content=prompt)],
                temperature=0.3,
                max_tokens=2048,
            )

            memories = json.loads(response.strip())

            if not isinstance(memories, list):
                return []

            return memories

        except Exception as e:
            print(f"Text extraction error: {e}")
            return []
