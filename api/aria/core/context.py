"""
ARIA - Context Builder

Phase: 2
Purpose: Assemble context for LLM including memories

Related Spec Sections:
- Section 2.2: Request Flow
- Section 3: Memory Architecture
"""

import asyncio
import logging
from typing import Optional
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.llm.base import Message
from aria.core.soul import soul_manager
from aria.core.tokenizer import count_tokens, get_default_max_context_tokens, truncate_to_budget
from aria.memory.short_term import ShortTermMemory
from aria.memory.long_term import LongTermMemory

logger = logging.getLogger(__name__)


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
        private: bool = False,
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
        llm_config = agent_config.get("llm", {})
        model_name = llm_config.get("model", "default")
        max_context_tokens = llm_config.get("max_context_tokens") or get_default_max_context_tokens(model_name)
        max_output_tokens = llm_config.get("max_tokens", 4096)
        input_budget = max(1024, max_context_tokens - max_output_tokens)
        try:
            conv_oid = ObjectId(conversation_id)
        except Exception:
            conv_oid = None
        conversation = await self.db.conversations.find_one(
            {"_id": conv_oid},
            {"summary": 1},
        ) if conv_oid else None

        # 1. System prompt
        system_prompt = agent_config.get("system_prompt", "You are a helpful assistant.")

        # Inject SOUL.md identity into system prompt
        soul_content = soul_manager.read()
        if soul_content:
            system_prompt += f"\n\n## Agent Identity (SOUL)\n\n{soul_content}"

        conversation_summary = conversation.get("summary") if conversation else None
        if conversation_summary:
            system_prompt += f"\n\n## Conversation Summary\n\n{conversation_summary}"

        # 2. Add long-term memories if enabled, in parallel with short-term fetch
        memory_config = agent_config.get("memory_config", {})
        short_term_messages = memory_config.get("short_term_messages", 20)

        memory_search_coro = None
        if include_memories and agent_config.get("capabilities", {}).get(
            "memory_enabled", True
        ):
            long_term_results = memory_config.get("long_term_results", 10)
            # Regular conversations exclude private memories (they go to cloud LLMs).
            # Private conversations can access all memories (everything stays local).
            memory_filters = None if private else {"private": {"$ne": True}}
            memory_search_coro = self.long_term.search(
                query=user_message, limit=long_term_results, filters=memory_filters
            )

        short_term_coro = self.short_term.get_current_conversation_context(
            conversation_id=conversation_id,
            max_messages=short_term_messages,
            max_tokens=input_budget,
            model=model_name,
        )

        # Run memory search and short-term fetch in parallel
        if memory_search_coro:
            relevant_memories, conversation_messages = await asyncio.gather(
                memory_search_coro, short_term_coro
            )
        else:
            relevant_memories = []
            conversation_messages = await short_term_coro

        memory_context = ""
        if relevant_memories:
            memory_texts = [
                f"- [{memory.content_type}] {memory.content}"
                for memory in relevant_memories
            ]
            memory_context = f"""

## Relevant Long-Term Memories

{chr(10).join(memory_texts)}

Use these memories to provide personalized and contextual responses.
"""
            # Fire-and-forget: batch access tracking off the hot path
            memory_ids = [m.id for m in relevant_memories]

            async def _track_access():
                try:
                    await self.long_term.batch_increment_access(memory_ids)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning("Failed to track memory access: %s", e)

            asyncio.create_task(_track_access())

        # Inject skill catalog (progressive disclosure: names + descriptions only)
        skill_context = ""
        if agent_config.get("capabilities", {}).get("tools_enabled", False):
            try:
                from aria.api.deps import _skill_registry
                if _skill_registry is not None:
                    skills = await _skill_registry.list_skills()
                    enabled_skills = [s for s in skills if s.get("enabled")]
                    if enabled_skills:
                        skill_lines = [
                            f"- **{s['name']}**: {s.get('description', 'No description')}"
                            for s in enabled_skills
                        ]
                        skill_context = (
                            "\n\n## Available Skills\n\n"
                            + "\n".join(skill_lines)
                            + "\n\nInvoke a skill by calling its tool. Full instructions will be provided upon invocation."
                        )
            except Exception as e:
                logger.debug("Skill catalog injection skipped: %s", e)

        # Inject ambient awareness context if enabled
        awareness_context = ""
        if settings.awareness_enabled and settings.awareness_inject_context:
            try:
                from aria.api.deps import _awareness_service
                if _awareness_service is not None:
                    context_lines = await _awareness_service.get_context_lines(limit=8, hours=1.0)
                    if context_lines:
                        awareness_context = (
                            "\n\n## Environmental Awareness\n\n"
                            "Recent observations from your environment:\n"
                            + "\n".join(f"- {line}" for line in context_lines)
                            + "\n\nUse these observations to provide contextually relevant responses. "
                            "You may reference them proactively if they seem relevant to the conversation."
                        )
            except Exception as e:
                logger.debug("Awareness context injection skipped: %s", e)

        # Inject deep_think delegation instructions if enabled
        delegation_context = ""
        if settings.deep_think_enabled:
            delegation_context = """

## Reasoning Delegation (IMPORTANT)

You are an orchestrator. Your job is to manage conversation flow, retrieve memories, and call tools.
For ALL substantive reasoning — answering questions, analysis, explanations, writing, planning,
code review, creative tasks — you MUST delegate to the `deep_think` tool.

How to use `deep_think`:
1. Gather all relevant context (user's question, memories, conversation history)
2. Call `deep_think` with a complete prompt that includes everything Claude needs to know
3. Relay Claude's response back to the user, optionally with light formatting

When NOT to use `deep_think`:
- Simple acknowledgments ("ok", "got it", "done")
- Tool routing (the user asks to run a shell command, fetch a URL, etc.)
- Memory lookups and status checks
- Following up on a previous deep_think response with minor clarification

When to ALWAYS use `deep_think`:
- The user asks a question that requires reasoning or knowledge
- The user asks for analysis, explanation, or opinion
- The user asks you to write something (text, code, plans)
- The user asks for help debugging or understanding something
- Any response where the quality of your thinking matters
"""

        # Combine system prompt with memory context, skills, awareness, and delegation
        full_system_prompt = system_prompt + memory_context + skill_context + awareness_context + delegation_context
        messages.append(Message(role="system", content=full_system_prompt))

        for msg in conversation_messages:
            messages.append(
                Message(
                    role=msg["role"],
                    content=msg["content"],
                    tool_call_id=msg.get("tool_call_id"),
                    name=msg.get("tool_name"),
                    tool_calls=msg.get("tool_calls"),
                )
            )

        # 4. Add current user message
        messages.append(Message(role="user", content=user_message))

        # Estimate token count before truncation for observability
        estimated_tokens = sum(count_tokens(m.content, model_name) + 4 for m in messages)
        if estimated_tokens > input_budget:
            logger.warning(
                "Context exceeds budget: %d tokens estimated vs %d budget — truncating",
                estimated_tokens, input_budget,
            )

        return truncate_to_budget(messages, input_budget, model_name)

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
