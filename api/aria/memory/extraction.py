"""
ARIA - Memory Extraction

Phase: 2
Purpose: Extract memories from conversations using LLM

Related Spec Sections:
- Section 3: Memory Architecture
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.llm.manager import llm_manager
from aria.llm.base import Message
from aria.core.prompts import load_prompt
from aria.core.resilience import retry_async
from aria.db.usage import UsageRepo
from aria.memory.long_term import LongTermMemory


class MemoryExtractor:
    """
    Extracts memories from conversations using LLM.
    Runs as background task after conversations.
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.long_term_memory = LongTermMemory(db)
        self.usage_repo = UsageRepo(db)

    async def extract_from_conversation(
        self,
        conversation_id: str,
        batch_size: int = 10,
        llm_backend: str = "llamacpp",
        llm_model: str = "default",
        private: bool = False,
    ) -> int:
        """
        Extract memories from unprocessed messages in a conversation.

        Args:
            conversation_id: Conversation ID
            batch_size: Number of messages to process at once
            llm_backend: LLM backend to use for extraction
            llm_model: LLM model to use
            private: Whether this is a private conversation (memories are isolated)

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
                conversation_id, batch, llm_backend, llm_model, private=private
            )
            total_extracted += extracted

        return total_extracted

    async def _extract_batch(
        self,
        conversation_id: str,
        messages: list[dict],
        llm_backend: str,
        llm_model: str,
        private: bool = False,
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

        prompt = load_prompt("extraction", messages=messages_text)

        # Extract memories — use ClaudeRunner if available, else API tokens
        response = None
        try:
            if settings.use_claude_runner and ClaudeRunner.is_available():
                runner = ClaudeRunner(timeout_seconds=settings.claude_runner_timeout_seconds)
                response = await runner.run(prompt)
                if not response:
                    logger.warning("ClaudeRunner returned no output for extraction, falling back to API")
                    response = await self._extract_via_api(prompt, llm_backend, llm_model, conversation_id)
            else:
                response = await self._extract_via_api(prompt, llm_backend, llm_model, conversation_id)

            if not response:
                return 0

            # Parse JSON response — strip markdown fences if present
            cleaned = response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                # Remove first line (```json or ```) and last line (```)
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines).strip()
            memories = json.loads(cleaned)

            if not isinstance(memories, list):
                logger.warning("Unexpected extraction response format: %s", response)
                return 0

            message_ids = [msg["id"] for msg in messages if "id" in msg]
            if not message_ids:
                logger.warning("No message IDs found in batch, skipping extraction")
                return 0

            # Create memories FIRST, then mark as processed.
            # This ensures messages are retried if memory creation fails.
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
                            "message_ids": message_ids,
                            "extracted_at": datetime.now(timezone.utc),
                        },
                        private=private,
                    )
                    extracted_count += 1
                except Exception as e:
                    logger.error("Error creating memory: %s", e)
                    continue

            # Mark messages as processed AFTER memories are created
            if extracted_count > 0:
                try:
                    await self.db.conversations.update_one(
                        {"_id": ObjectId(conversation_id)},
                        {"$set": {"messages.$[elem].memory_processed": True}},
                        array_filters=[{"elem.id": {"$in": message_ids}}],
                    )
                except Exception as e:
                    logger.error("Failed to mark messages as processed: %s", e)
                    # Memories were created; duplicates on next run are preferable
                    # to data loss from never retrying.

            return extracted_count

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse extraction response: %s", e)
            logger.warning("Response was: %s", response)
            return 0
        except Exception as e:
            logger.error("Memory extraction error: %s", e)
            return 0

    async def _extract_via_api(
        self,
        prompt: str,
        llm_backend: str,
        llm_model: str,
        conversation_id: Optional[str] = None,
    ) -> Optional[str]:
        """Run extraction via LLM API adapter (consumes API tokens)."""
        adapter = llm_manager.get_adapter(llm_backend, llm_model)
        response, _, usage = await retry_async(
            lambda: adapter.complete(
                messages=[Message(role="user", content=prompt)],
                temperature=0.3,
                max_tokens=2048,
            ),
            retries=3,
            base_delay=1.0,
        )
        if usage and conversation_id:
            await self.usage_repo.record(
                model=llm_model,
                source="memory_extraction",
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                conversation_id=conversation_id,
                metadata={"backend": llm_backend},
            )
        return response

    async def extract_from_text(
        self,
        text: str,
        llm_backend: str = "llamacpp",
        llm_model: str = "default",
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
        prompt = load_prompt("extraction", messages=f"USER: {text}")

        try:
            if settings.use_claude_runner and ClaudeRunner.is_available():
                runner = ClaudeRunner(timeout_seconds=settings.claude_runner_timeout_seconds)
                response = await runner.run(prompt)
                if not response:
                    response = await self._extract_via_api(prompt, llm_backend, llm_model)
            else:
                response = await self._extract_via_api(prompt, llm_backend, llm_model)

            if not response:
                return []

            memories = json.loads(response.strip())

            if not isinstance(memories, list):
                return []

            return memories

        except Exception as e:
            logger.error("Text extraction error: %s", e)
            return []
