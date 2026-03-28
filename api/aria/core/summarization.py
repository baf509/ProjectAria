"""
ARIA - Conversation Summarization

Purpose: Generate and persist rolling conversation summaries.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.core.prompts import load_prompt
from aria.core.resilience import retry_async
from aria.llm.base import LLMAdapter, Message


def _format_messages(messages: list[dict]) -> str:
    return "\n".join(f"{msg.get('role', 'unknown').upper()}: {msg.get('content', '')}" for msg in messages)


async def summarize_conversation(messages: list[dict], llm: LLMAdapter) -> str:
    """Summarize a list of conversation messages.

    Uses ClaudeRunner (subscription tokens) when available,
    falls back to the provided LLM adapter (API tokens).
    """
    if not messages:
        return ""

    prompt = load_prompt("summarization", conversation=_format_messages(messages))

    if settings.use_claude_runner and ClaudeRunner.is_available():
        runner = ClaudeRunner(timeout_seconds=settings.claude_runner_timeout_seconds)
        result = await runner.run(prompt)
        if result:
            return result.strip()
        # Fall through to API on failure

    async def do_complete():
        return await llm.complete(
            messages=[Message(role="user", content=prompt)],
            temperature=0.3,
            max_tokens=256,
        )

    response, _, _ = await retry_async(do_complete, retries=3, base_delay=1.0)
    return response.strip()


async def compact_conversation(
    messages: list[dict],
    llm: LLMAdapter,
    existing_summary: str | None = None,
) -> str:
    """Produce a structured compaction of conversation messages.

    Uses the structured_compaction prompt which preserves goal, constraints,
    progress, decisions, files touched, open questions, and key context.
    Falls back to basic summarization if the structured prompt fails.
    """
    if not messages:
        return ""

    prev_section = ""
    if existing_summary:
        prev_section = f"Previous summary to incorporate:\n{existing_summary}"

    prompt = load_prompt(
        "structured_compaction",
        conversation=_format_messages(messages),
        previous_summary=prev_section,
    )

    result = None
    if settings.use_claude_runner and ClaudeRunner.is_available():
        runner = ClaudeRunner(timeout_seconds=settings.claude_runner_timeout_seconds)
        result = await runner.run(prompt)

    if not result:
        async def do_complete():
            return await llm.complete(
                messages=[Message(role="user", content=prompt)],
                temperature=0.3,
                max_tokens=600,
            )
        result, _, _ = await retry_async(do_complete, retries=3, base_delay=1.0)

    return result.strip() if result else ""


async def maybe_update_conversation_summary(
    db: AsyncIOMotorDatabase,
    conversation_id: str,
    *,
    llm: LLMAdapter,
    short_term_messages: int,
) -> str | None:
    """Compact older messages when the conversation exceeds the short-term window.

    Uses structured compaction for richer context preservation.
    """
    conversation = await db.conversations.find_one(
        {"_id": ObjectId(conversation_id)},
        {"messages": 1, "summary": 1},
    )
    if not conversation:
        return None

    messages = conversation.get("messages", [])
    if len(messages) <= short_term_messages:
        return conversation.get("summary")

    dropped_messages = messages[:-short_term_messages]
    existing_summary = conversation.get("summary")

    summary = await compact_conversation(dropped_messages, llm, existing_summary)
    if not summary:
        return existing_summary

    await db.conversations.update_one(
        {"_id": ObjectId(conversation_id)},
        {
            "$set": {
                "summary": summary,
                "summary_updated_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    return summary
