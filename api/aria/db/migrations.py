"""
ARIA - MongoDB Migrations

Purpose: Idempotent index creation for startup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import OperationFailure

from aria.config import settings

logger = logging.getLogger(__name__)


async def run_migrations(db: AsyncIOMotorDatabase) -> None:
    """Run startup migrations."""
    await _ensure_schema_validation(db)
    await _ensure_standard_indexes(db)
    await _ensure_search_indexes(db)
    await _seed_pi_coding_agent(db)
    await _seed_search_agent(db)


async def _ensure_schema_validation(db: AsyncIOMotorDatabase) -> None:
    """Apply $jsonSchema validators to core collections.

    Uses 'warn' validation action so invalid documents are logged but not rejected,
    avoiding breakage during schema evolution.
    """
    schemas: dict[str, dict] = {
        "conversations": {
            "bsonType": "object",
            "required": ["agent_id", "status", "created_at", "updated_at", "messages"],
            "properties": {
                "agent_id": {"bsonType": "objectId"},
                "status": {"bsonType": "string", "enum": ["active", "archived"]},
                "title": {"bsonType": "string"},
                "messages": {"bsonType": "array"},
                "created_at": {"bsonType": "date"},
                "updated_at": {"bsonType": "date"},
            },
        },
        "memories": {
            "bsonType": "object",
            "required": ["content", "content_type", "created_at"],
            "properties": {
                "content": {"bsonType": "string"},
                "content_type": {"bsonType": "string"},
                "categories": {"bsonType": "array"},
                "importance": {"bsonType": "double"},
                "created_at": {"bsonType": "date"},
            },
        },
        "agents": {
            "bsonType": "object",
            "required": ["name", "slug", "llm"],
            "properties": {
                "name": {"bsonType": "string"},
                "slug": {"bsonType": "string"},
                "llm": {"bsonType": "object"},
            },
        },
        "schedules": {
            "bsonType": "object",
            "required": ["name", "schedule_type", "action", "enabled", "next_run_at"],
            "properties": {
                "name": {"bsonType": "string"},
                "schedule_type": {"bsonType": "string", "enum": ["once", "recurring"]},
                "action": {"bsonType": "string"},
                "enabled": {"bsonType": "bool"},
                "next_run_at": {"bsonType": "date"},
            },
        },
    }

    existing_collections = set(await db.list_collection_names())

    for coll_name, schema in schemas.items():
        try:
            if coll_name in existing_collections:
                await db.command(
                    "collMod",
                    coll_name,
                    validator={"$jsonSchema": schema},
                    validationLevel="moderate",
                    validationAction="warn",
                )
            else:
                await db.create_collection(
                    coll_name,
                    validator={"$jsonSchema": schema},
                    validationLevel="moderate",
                    validationAction="warn",
                )
            logger.info("Applied schema validation for collection: %s", coll_name)
        except OperationFailure as exc:
            logger.warning("Could not apply schema validation for %s: %s", coll_name, exc)
        except Exception as exc:
            logger.warning("Schema validation setup failed for %s: %s", coll_name, exc)


async def _ensure_standard_indexes(db: AsyncIOMotorDatabase) -> None:
    """Create standard MongoDB indexes used by the application."""
    await _safe_create_index(db.conversations, "updated_at", name="conversation_updated_at")
    await _safe_create_index(db.conversations, "status", name="conversation_status")
    await _safe_create_index(
        db.conversations,
        [("pinned", -1), ("updated_at", -1)],
        name="conversation_pinned_updated_at",
    )

    await _safe_create_index(db.memories, "status", name="memory_status")
    await _safe_create_index(db.memories, "created_at", name="memory_created_at")
    await _safe_create_index(db.memories, "last_accessed_at", name="memory_last_accessed_at")
    await _safe_create_index(db.memories, "access_count", name="memory_access_count")
    await _safe_create_index(db.memories, "content_type", name="memory_content_type")
    await _safe_create_index(db.memories, "categories", name="memory_categories")
    await _safe_create_index(db.usage, "timestamp", name="usage_timestamp")
    await _safe_create_index(db.usage, "model", name="usage_model")
    await _safe_create_index(db.usage, "source", name="usage_source")
    await _safe_create_index(db.usage, "agent_slug", name="usage_agent_slug")
    await _safe_create_index(db.usage, "conversation_id", name="usage_conversation_id")
    await _safe_create_index(db.signal_contacts, "sender", name="signal_contact_sender", unique=True)
    await _safe_create_index(db.signal_contacts, "conversation_id", name="signal_contact_conversation_id")
    await _safe_create_index(db.background_tasks, "status", name="background_task_status")
    await _safe_create_index(db.background_tasks, "created_at", name="background_task_created_at")
    await _safe_create_index(db.research_runs, "status", name="research_run_status")
    await _safe_create_index(db.research_runs, "created_at", name="research_run_created_at")
    await _safe_create_index(db.research_runs, "task_id", name="research_run_task_id", unique=True, sparse=True)
    await _safe_create_index(db.coding_sessions, "status", name="coding_session_status")
    await _safe_create_index(db.coding_sessions, "created_at", name="coding_session_created_at")
    await _safe_create_index(db.coding_sessions, "backend", name="coding_session_backend")
    await _safe_create_index(db.session_reports, "session_id", name="session_report_session_id", unique=True)
    await _safe_create_index(db.session_reports, "created_at", name="session_report_created_at")
    await _safe_create_index(db.workflows, "name", name="workflow_name", unique=True)
    await _safe_create_index(db.workflows, "created_at", name="workflow_created_at")
    await _safe_create_index(db.workflow_runs, "workflow_id", name="workflow_run_workflow_id")
    await _safe_create_index(db.workflow_runs, "status", name="workflow_run_status")
    await _safe_create_index(db.audit_logs, "timestamp", name="audit_timestamp")
    await _safe_create_index(db.audit_logs, [("category", 1), ("timestamp", -1)], name="audit_category_timestamp")
    await _safe_create_index(db.audit_logs, [("status", 1), ("timestamp", -1)], name="audit_status_timestamp")

    await _safe_create_index(db.agents, "slug", name="agent_slug", unique=True)

    await _safe_create_index(db.dream_journal, "created_at", name="dream_journal_created")
    await _safe_create_index(
        db.dream_soul_proposals,
        [("status", 1), ("created_at", -1)],
        name="soul_proposals_status_created",
    )

    await _safe_create_index(db.tool_audit, "created_at", name="audit_created")
    await _safe_create_index(
        db.tool_audit,
        [("tool_name", 1), ("created_at", -1)],
        name="audit_tool_created",
    )

    # Watched Shells subsystem
    await _safe_create_index(db.shells, "name", name="shells_name", unique=True)
    await _safe_create_index(db.shells, "last_activity_at", name="shells_last_activity")
    await _safe_create_index(
        db.shells,
        [("status", 1), ("last_activity_at", -1)],
        name="shells_status_activity",
    )
    await _safe_create_index(
        db.shell_events,
        [("shell_name", 1), ("line_number", 1)],
        name="shell_events_name_line",
    )
    await _safe_create_index(
        db.shell_events,
        [("shell_name", 1), ("ts", 1)],
        name="shell_events_name_ts",
    )
    await _safe_create_index(
        db.shell_events,
        [("shell_name", 1), ("kind", 1), ("ts", -1)],
        name="shell_events_name_kind_ts",
    )
    try:
        await db.shell_events.create_index([("text_clean", "text")], name="shell_events_text")
    except OperationFailure as exc:
        logger.info("shell_events text index skipped: %s", (exc.details or {}).get("errmsg", exc))
    await _safe_create_index(
        db.shell_snapshots,
        [("shell_name", 1), ("ts", -1)],
        name="shell_snapshots_name_ts",
    )
    await _safe_create_index(
        db.shell_snapshots,
        [("shell_name", 1), ("content_hash", 1)],
        name="shell_snapshots_name_hash",
    )
    await _safe_create_index(
        db.shell_extraction_state,
        "shell_name",
        name="shell_extraction_state_name",
        unique=True,
    )
    logger.info("Standard MongoDB indexes ensured")


async def _safe_create_index(collection, keys, **kwargs) -> None:
    """Create an index without failing startup on equivalent pre-existing indexes."""
    try:
        await collection.create_index(keys, **kwargs)
    except OperationFailure as exc:
        if exc.code == 85:
            logger.info("Skipping existing conflicting index on %s: %s", collection.name, (exc.details or {}).get("errmsg"))
            return
        raise


async def _ensure_search_indexes(db: AsyncIOMotorDatabase) -> None:
    """
    Best-effort Atlas Search / Vector Search index creation.

    This is skipped automatically when the deployment does not support
    search index management via the current driver/server combination.
    """
    memories = db.memories
    if not hasattr(memories, "list_search_indexes") or not hasattr(memories, "create_search_index"):
        logger.info("Search index management not available in this environment; skipping")
        return

    try:
        existing = await memories.list_search_indexes().to_list(length=None)
        existing_names = {index.get("name") for index in existing}
    except Exception as exc:
        logger.warning("Could not list search indexes; skipping search index setup: %s", exc)
        return

    if "memory_text_index" not in existing_names:
        try:
            await memories.create_search_index(
                {
                    "name": "memory_text_index",
                    "definition": {
                        "mappings": {
                            "dynamic": False,
                            "fields": {
                                "content": {"type": "string"},
                                "categories": {"type": "string"},
                            },
                        }
                    },
                }
            )
            logger.info("Created search index: memory_text_index")
        except Exception as exc:
            logger.warning("Could not create memory_text_index: %s", exc)

    if "memory_vector_index" not in existing_names:
        try:
            await memories.create_search_index(
                {
                    "name": "memory_vector_index",
                    "type": "vectorSearch",
                    "definition": {
                        "fields": [
                            {
                                "type": "vector",
                                "path": "embedding",
                                "numDimensions": settings.embedding_dimension,
                                "similarity": "cosine",
                            },
                            {
                                "type": "filter",
                                "path": "status",
                            },
                            {
                                "type": "filter",
                                "path": "content_type",
                            },
                            {
                                "type": "filter",
                                "path": "categories",
                            },
                        ]
                    },
                }
            )
            logger.info("Created search index: memory_vector_index")
        except Exception as exc:
            logger.warning("Could not create memory_vector_index: %s", exc)


_PI_CODING_SYSTEM_PROMPT = """\
You are the Pi Coding Agent, a focused software development assistant running on a local LLM.

## Core Approach

You follow a structured coding workflow inspired by best practices:

1. **Understand First**: Before writing code, clarify the requirements. Ask questions if the task is ambiguous.
2. **Plan Before Coding**: Outline your approach — what files to touch, what changes to make, what order.
3. **Incremental Changes**: Make small, testable changes rather than large rewrites.
4. **Explain Decisions**: When you make a design choice, explain why.

## Capabilities

- Code generation, debugging, and refactoring
- Architecture design and code review
- Explaining complex code and concepts
- Writing tests and documentation
- Analyzing error messages and stack traces

## Interaction Style

- Be direct and concise — skip pleasantries in favor of actionable output
- Use code blocks with language tags for all code
- When showing changes, indicate which file and what changed
- If a task is too large for one response, break it into steps and tackle them in order
- Proactively suggest improvements you notice, but stay focused on the requested task

## Constraints

- You run on a local LLM — be mindful of context window limits
- Focus on one task at a time for best results
- If you need more context (file contents, error logs), ask for it explicitly
"""


async def _seed_pi_coding_agent(db: AsyncIOMotorDatabase) -> None:
    """Ensure the Pi Coding Agent exists (idempotent)."""
    existing = await db.agents.find_one({"slug": "pi-coding"})
    if existing:
        return

    now = datetime.now(timezone.utc)
    agent = {
        "name": "Pi Coding Agent",
        "slug": "pi-coding",
        "description": "Local LLM coding assistant inspired by pi-mono. Structured thinking, incremental changes, clear explanations.",
        "system_prompt": _PI_CODING_SYSTEM_PROMPT,
        "mode_category": "coding",
        "greeting": "Pi Coding Agent ready. What are we building?",
        "context_instructions": None,
        "llm": {
            "backend": "llamacpp",
            "model": "default",
            "temperature": 0.4,
            "max_tokens": 4096,
            "max_context_tokens": None,
            "force_non_streaming": False,
        },
        "fallback_chain": [],
        "capabilities": {
            "memory_enabled": True,
            "tools_enabled": True,
            "computer_use_enabled": False,
        },
        "mode_metadata": {
            "icon": "code",
            "color": "#22c55e",
            "keywords": ["code", "coding", "debug", "refactor", "programming", "dev"],
            "keyboard_shortcut": None,
        },
        "memory_config": {
            "auto_extract": True,
            "short_term_messages": 20,
            "long_term_results": 5,
            "categories_filter": None,
        },
        "enabled_tools": ["filesystem", "shell", "web", "claude_agent", "pi_coding_agent", "deep_think"],
        "is_default": False,
        "created_at": now,
        "updated_at": now,
    }

    await db.agents.insert_one(agent)
    logger.info("Seeded Pi Coding Agent (slug=pi-coding, backend=llamacpp)")


_SEARCH_AGENT_SYSTEM_PROMPT = """You are ARIA's Search Agent.

You drive the `search_agent` tool, powered by the local chromadb/context-1
model. Your job is to find the documents most relevant to a user's
information need — across ARIA's long-term memory, the web, and local
files — and return a concise ranked summary with citations.

Guidelines:
- Always invoke `search_agent` first. Do not answer from prior knowledge alone.
- Cite retrieved documents by id (mem:, web:, or file:) in your final summary.
- If the user wants synthesis or a report, pass the ranked documents to
  `deep_think` or the research flow rather than synthesizing yourself.
- Prefer precision. Call out when retrieval returned nothing useful.
"""


async def _seed_search_agent(db: AsyncIOMotorDatabase) -> None:
    """Ensure the Search Agent profile exists (idempotent)."""
    existing = await db.agents.find_one({"slug": "search-agent"})
    if existing:
        return

    now = datetime.now(timezone.utc)
    agent = {
        "name": "Search Agent",
        "slug": "search-agent",
        "description": "Agentic retrieval over ARIA memory, the web, and local files, driven by the local chromadb/context-1 model.",
        "system_prompt": _SEARCH_AGENT_SYSTEM_PROMPT,
        "mode_category": "research",
        "greeting": "Search Agent ready. What are you looking for?",
        "context_instructions": None,
        "llm": {
            "backend": "context1",
            "model": "default",
            "temperature": 0.3,
            "max_tokens": 2048,
            "max_context_tokens": None,
            "force_non_streaming": False,
        },
        "fallback_chain": [],
        "capabilities": {
            "memory_enabled": True,
            "tools_enabled": True,
            "computer_use_enabled": False,
        },
        "mode_metadata": {
            "icon": "search",
            "color": "#38bdf8",
            "keywords": ["search", "find", "lookup", "retrieve", "research"],
            "keyboard_shortcut": None,
        },
        "memory_config": {
            "auto_extract": False,
            "short_term_messages": 10,
            "long_term_results": 0,  # the tool handles retrieval itself
            "categories_filter": None,
        },
        "enabled_tools": ["search_agent", "web", "filesystem", "deep_think"],
        "is_default": False,
        "created_at": now,
        "updated_at": now,
    }

    await db.agents.insert_one(agent)
    logger.info("Seeded Search Agent (slug=search-agent, backend=context1)")
