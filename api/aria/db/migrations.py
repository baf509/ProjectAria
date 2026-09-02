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
    await _seed_pi_coding_ridge_agent(db)
    await _seed_search_agent(db)
    await _normalize_project_status(db)


async def _normalize_project_status(db: AsyncIOMotorDatabase) -> None:
    """Reconcile the two project status axes after the aria-shells merge.

    The harvester (carved out in aria-shells) used to write the machine activity
    value ('active'/'idle') into the human `status` field, but ProjectAria's
    Project model treats `status` as the lifecycle (active/paused/archived) and
    keeps activity in `activity_status`. Any legacy doc whose `status` is 'idle'
    (or otherwise not a valid lifecycle value) would fail model deserialization,
    so promote those to 'active' while preserving their activity in
    `activity_status`. Idempotent — a no-op once converged.
    """
    valid = {"active", "paused", "archived"}
    try:
        cursor = db.projects.find(
            {"status": {"$nin": list(valid)}},
            {"status": 1, "activity_status": 1},
        )
        fixed = 0
        async for doc in cursor:
            legacy = doc.get("status")
            update = {"status": "active"}
            # Preserve the legacy activity signal if it wasn't already recorded.
            if not doc.get("activity_status") and legacy in {"active", "idle"}:
                update["activity_status"] = legacy
            await db.projects.update_one({"_id": doc["_id"]}, {"$set": update})
            fixed += 1
        if fixed:
            logger.info("Normalized status on %d legacy project doc(s)", fixed)
    except Exception as exc:  # pragma: no cover - non-fatal
        logger.warning("Project status normalization failed: %s", exc)


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
    # Backfill queue (memory/backfill.py). Partial so it indexes only the docs
    # that are actually waiting — normally zero, and at most a short outage's
    # worth — instead of carrying an entry for every memory in the collection.
    await _safe_create_index(
        db.memories,
        "embedding_pending",
        name="memory_embedding_pending",
        partialFilterExpression={"embedding_pending": True},
    )
    await _ensure_usage_ttl(db)
    await _safe_create_index(
        db.conversation_archives, "conversation_id",
        name="conversation_archive_conversation_id",
    )
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

    # Multi-machine fleet: aria-node registry + the per-node command queue.
    await _safe_create_index(db.nodes, "last_heartbeat_at", name="node_last_heartbeat_at")
    await _safe_create_index(
        db.shell_commands, [("node_id", 1), ("status", 1)], name="shell_command_node_status"
    )
    await _safe_create_index(db.shell_commands, "created_at", name="shell_command_created_at")
    # TTL: expire finished/stale commands at their per-doc expires_at.
    await _safe_create_index(
        db.shell_commands, "expires_at", name="shell_command_ttl", expireAfterSeconds=0
    )

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
        [("shell_name", 1), ("event_id", 1)],
        name="shell_events_node_event_id",
        unique=True,
        # A sparse compound index still contains legacy documents because
        # shell_name exists, representing the missing event_id as null. Use a
        # partial index so only node-spool events participate in uniqueness.
        partialFilterExpression={"event_id": {"$type": "string"}},
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


async def _ensure_usage_ttl(db: AsyncIOMotorDatabase) -> None:
    """Keep `usage_timestamp` as the collection's TTL index.

    The usage collection grows one row per LLM request and nothing expired
    them. A TTL index also serves ordinary range queries, so this is the same
    index the cost routes already use -- not a second one.

    It has to be a collMod rather than a create: an index already exists on
    `timestamp`, and creating a second one with different options fails with
    IndexOptionsConflict (85), which _safe_create_index swallows -- so a plain
    create_index would silently never apply the TTL.
    """
    days = int(getattr(settings, "usage_retention_days", 0) or 0)
    await _safe_create_index(db.usage, "timestamp", name="usage_timestamp")
    if days <= 0:
        return
    expire_seconds = days * 86400
    try:
        existing = await db.usage.list_indexes().to_list(length=100)
    except Exception as exc:  # pragma: no cover - inspection is best-effort
        logger.warning("Could not inspect usage indexes for TTL: %s", exc)
        return
    for idx in existing:
        if idx.get("name") != "usage_timestamp":
            continue
        if idx.get("expireAfterSeconds") == expire_seconds:
            return  # already correct
        try:
            await db.command({
                "collMod": "usage",
                "index": {"name": "usage_timestamp", "expireAfterSeconds": expire_seconds},
            })
            logger.info("usage retention set to %d days (TTL on usage_timestamp)", days)
        except Exception as exc:
            logger.warning("Could not set usage TTL (%d days): %s", days, exc)
        return


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
- Directly reading, editing, and writing files through Pi's native tools
- Running shell commands and verification checks in the assigned workspace

## Interaction Style

- Be direct and concise — skip pleasantries in favor of actionable output
- Use code blocks with language tags for all code
- When showing changes, indicate which file and what changed
- If a task is too large for one response, break it into steps and tackle them in order
- Proactively suggest improvements you notice, but stay focused on the requested task

## Constraints

- You run on a local LLM — be mindful of context window limits
- Focus on one task at a time for best results
- Use Pi's tools to inspect available files and logs before asking the user for
  context that is already present in the workspace
"""


_PI_CODING_RIDGE_SYSTEM_PROMPT = """\
You are the Pi Coding Agent (Ridge) — a hands-on software development agent.

## Where you run — read this carefully

Your **thinking happens on Ridge**, a separate machine with an RTX 3090, running
Qwen3.6-35B-A3B. Your **hands are on corsair-ai**. Every tool you call —
filesystem reads and writes, shell commands, tests — executes on corsair-ai's
local disk, NOT on Ridge. Ridge has no copy of these repositories and you must
never try to reach it directly.

Practically: when you write a file, it lands on corsair-ai. When you run a
command, it runs on corsair-ai. Reason about paths accordingly.

## You actually change code

You run as the real Pi coding CLI and have Pi's native read, write, edit, and
bash tools. Read the real file before editing it; do not guess at contents or
invent APIs. After a change, VERIFY it rather than asserting success. Prefer
the cheapest project check that would catch your mistake.

If a check fails, report the failure and its actual output. Never claim a change
works when the check did not pass, and never describe a test as passing that you
did not run.

## Working style

1. **Understand first.** Read the relevant files. Ambiguity is worth one question;
   guessing is not.
2. **Plan, then act.** Say what you intend to touch before touching it.
3. **Small, verifiable steps.** Prefer an edit you can check over a large rewrite
   you cannot.
4. **Report honestly.** If a test fails, say so and show the output. If you
   skipped something, say that. Never claim a change works when you have not run
   it.
5. **Match the surrounding code** — its naming, its idiom, its comment density.

## Operating constraints specific to this agent

- **Ridge sleeps when idle.** Your first call after a quiet period wakes it by
  Wake-on-LAN and can take ~90 seconds before anything comes back. That is normal
  and is not a failure — do not retry it as though it errored.
- **One request at a time.** The Ridge engine has no continuous batching, so
  parallel calls queue behind each other. Work sequentially; do not fan out.
- **Your reasoning is verbose and counts against your budget.** Think, but get to
  the actionable output — a truncated answer helps nobody.
- **Context is 147456 tokens** and it includes the files you read. Read what you
  need, not whole trees.
"""


async def _seed_pi_coding_ridge_agent(db: AsyncIOMotorDatabase) -> None:
    """Keep the old slug as a Flash-Next compatibility profile.

    Pi inference is restricted to Corsair's two Qwen deployments through ARIA;
    the historical Ridge route is no longer a valid Pi provider.
    """
    existing = await db.agents.find_one({"slug": "pi-coding-ridge"})
    if existing:
        return

    now = datetime.now(timezone.utc)
    agent = {
        "name": "Pi Coding Agent (Flash Next via ARIA)",
        "slug": "pi-coding-ridge",
        "description": (
            "Hands-on coding agent using Qwen3.8 Flash Next on Corsair through "
            "ARIA's inference gateway. The real Pi CLI runs in an ARIA shell."
        ),
        "system_prompt": _PI_CODING_SYSTEM_PROMPT,
        "mode_category": "coding",
        "greeting": "Pi Coding (Flash Next via ARIA) ready. What are we building?",
        "context_instructions": None,
        "llm": {
            "backend": "aria",
            "model": "Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K",
            "temperature": 0.3,
            "max_tokens": 8192,
            "max_context_tokens": 253952,
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
            "color": "#f97316",
            "keywords": ["flash-next", "aria", "qwen", "code", "coding", "local-gpu"],
            "keyboard_shortcut": None,
        },
        "memory_config": {
            "auto_extract": True,
            "short_term_messages": 20,
            "long_term_results": 5,
            "categories_filter": None,
        },
        # Legacy agent-schema fields retained for launch-profile compatibility;
        # external Pi supplies its own tools rather than ARIA's ToolRouter.
        "enabled_tools": ["filesystem", "shell", "web", "deep_think"],
        "is_default": False,
        "created_at": now,
        "updated_at": now,
    }

    await db.agents.insert_one(agent)
    logger.info("Seeded Pi Coding Agent Flash compatibility profile through ARIA")


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
        # This legacy db.agents row is a launch profile for the external Pi CLI.
        # Pi carries only the two Corsair Qwen models, both through ARIA.
        "llm": {
            "backend": "aria",
            "model": "Qwen3.8-27B-R9700-Radiance",
            "temperature": 0.4,
            "max_tokens": 4096,
            "max_context_tokens": 245760,
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
