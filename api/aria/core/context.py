"""
ARIA - Context Builder

Phase: 2
Purpose: Assemble context for LLM including memories

Related Spec Sections:
- Section 2.2: Request Flow
- Section 3: Memory Architecture
"""

from typing import Optional
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.llm.base import Message
from aria.memory.short_term import ShortTermMemory
from aria.memory.long_term import LongTermMemory


class ContextBuilder:
    """
    Assembles context for LLM requests including:
    - System prompt from agent
    - Short-term memory (recent conversation)
    - Long-term memory (relevant facts/preferences)
    - Current user message
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.short_term = ShortTermMemory(db)
        self.long_term = LongTermMemory(db)

    async def build_messages(
        self,
        conversation_id: str,
        user_message: str,
        agent_config: dict,
        include_memories: bool = True,
    ) -> list[Message]:
        """
        Build complete message list for LLM.

        Args:
            conversation_id: Conversation ID
            user_message: Current user message
            agent_config: Agent configuration dict
            include_memories: Whether to include long-term memories

        Returns:
            List of Message objects ready for LLM
        """
        messages = []

        # 1. System prompt
        system_prompt = agent_config["system_prompt"]

        # 2. Add long-term memories if enabled
        memory_context = ""
        if include_memories and agent_config.get("capabilities", {}).get(
            "memory_enabled", False
        ):
            memory_config = agent_config.get("memory_config", {})
            long_term_results = memory_config.get("long_term_results", 10)

            # Search for relevant memories
            relevant_memories = await self.long_term.search(
                query=user_message, limit=long_term_results
            )

            if relevant_memories:
                memory_texts = []
                for memory in relevant_memories:
                    # Track access
                    await self.long_term.increment_access(memory.id)

                    memory_texts.append(
                        f"- [{memory.content_type}] {memory.content}"
                    )

                memory_context = f"""

## Relevant Long-Term Memories

{chr(10).join(memory_texts)}

Use these memories to provide personalized and contextual responses.
"""

        # Combine system prompt with memory context
        full_system_prompt = system_prompt + memory_context
        messages.append(Message(role="system", content=full_system_prompt))

        # 3. Add short-term context (conversation history)
        memory_config = agent_config.get("memory_config", {})
        short_term_messages = memory_config.get("short_term_messages", 20)

        conversation_messages = await self.short_term.get_current_conversation_context(
            conversation_id=conversation_id,
            max_messages=short_term_messages,
        )

        for msg in conversation_messages:
            messages.append(
                Message(
                    role=msg["role"],
                    content=msg["content"],
                )
            )

        # 4. Add current user message
        messages.append(Message(role="user", content=user_message))

        return messages

    async def get_recent_context_summary(
        self, hours: int = 24, limit: int = 5
    ) -> str:
        """
        Get a summary of recent conversations for context.

        Args:
            hours: Look back this many hours
            limit: Maximum conversations to include

        Returns:
            Formatted context summary
        """
        summaries = await self.short_term.get_recent_conversations_context(
            hours=hours, limit=limit
        )

        if not summaries:
            return "No recent conversations."

        lines = ["Recent conversations:"]
        for summary in summaries:
            title = summary.title
            date = summary.updated_at.strftime("%Y-%m-%d %H:%M")
            if summary.summary:
                lines.append(f"- {title} ({date}): {summary.summary}")
            else:
                lines.append(f"- {title} ({date})")

        return "\n".join(lines)
