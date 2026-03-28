"""
ARIA - AgentBenchPlatform Data Migration

Phase: 20
Purpose: Migrate memories and usage data from ABP's MongoDB database to ARIA's format.

The migration is idempotent — records are tagged with a source marker so
re-running the migration skips already-migrated documents.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from aria.config import settings
from aria.memory.embeddings import embedding_service

logger = logging.getLogger(__name__)

ABP_DATABASE = "agentbenchplatform"
MIGRATION_SOURCE_TAG = "abp_migration"


async def _get_abp_db() -> tuple[AsyncIOMotorClient, AsyncIOMotorDatabase] | tuple[None, None]:
    """Return the ABP client and database handle, or (None, None) if it doesn't exist."""
    client: AsyncIOMotorClient = AsyncIOMotorClient(settings.mongodb_uri)
    db_names = await client.list_database_names()
    if ABP_DATABASE not in db_names:
        logger.warning("ABP database '%s' not found — skipping migration", ABP_DATABASE)
        client.close()
        return None, None
    return client, client[ABP_DATABASE]


async def migrate_memories_from_abp(
    source_db: AsyncIOMotorDatabase,
    target_db: AsyncIOMotorDatabase,
) -> dict[str, Any]:
    """Copy memories from ABP's ``memories`` collection into ARIA's format.

    ABP memory documents have fields:
        key, content, scope, task_id, session_id, content_type, embedding,
        metadata, created_at, updated_at

    ARIA memory documents have fields:
        content, content_type, categories, importance, status, embedding,
        source, conversation_id, created_at, last_accessed_at, access_count,
        metadata

    Mapping:
        content       -> content
        content_type   -> content_type (kept as-is; ARIA uses same strings)
        scope/task_id  -> stored in metadata for provenance
        embedding      -> re-embedded if missing, kept if 1024-dim
        key            -> stored in metadata.abp_key
        metadata       -> merged into metadata
        created_at     -> created_at
    """
    stats = {"examined": 0, "migrated": 0, "skipped_duplicate": 0, "skipped_empty": 0, "errors": 0}

    source_col = source_db["memories"]
    target_col = target_db["memories"]

    cursor = source_col.find({})
    async for doc in cursor:
        stats["examined"] += 1
        content = doc.get("content", "").strip()
        if not content:
            stats["skipped_empty"] += 1
            continue

        # Idempotency: check if we already migrated this exact document
        abp_id = str(doc["_id"])
        existing = await target_col.find_one({"metadata.abp_source_id": abp_id})
        if existing:
            stats["skipped_duplicate"] += 1
            continue

        try:
            # Build the ARIA-format document
            abp_metadata = doc.get("metadata") or {}
            aria_metadata = {
                **abp_metadata,
                "abp_source_id": abp_id,
                "abp_key": doc.get("key", ""),
                "abp_scope": doc.get("scope", ""),
                "abp_task_id": doc.get("task_id", ""),
                "abp_session_id": doc.get("session_id", ""),
                "migration_source": MIGRATION_SOURCE_TAG,
                "migrated_at": datetime.now(timezone.utc).isoformat(),
            }

            # Handle embedding: keep if correct dimension, otherwise re-embed
            embedding = doc.get("embedding")
            if embedding and len(embedding) != settings.embedding_dimension:
                embedding = None
            if not embedding:
                try:
                    embedding = await embedding_service.embed(content)
                except Exception as emb_err:
                    logger.warning("Could not embed ABP memory %s: %s", abp_id, emb_err)
                    embedding = None

            aria_doc = {
                "content": content,
                "content_type": doc.get("content_type", "fact"),
                "categories": abp_metadata.get("categories", []),
                "importance": abp_metadata.get("importance", 0.5),
                "status": "active",
                "embedding": embedding,
                "source": MIGRATION_SOURCE_TAG,
                "conversation_id": None,
                "created_at": doc.get("created_at", datetime.now(timezone.utc)),
                "last_accessed_at": doc.get("updated_at", datetime.now(timezone.utc)),
                "access_count": 0,
                "metadata": aria_metadata,
            }

            await target_col.insert_one(aria_doc)
            stats["migrated"] += 1
        except Exception:
            logger.exception("Failed to migrate ABP memory %s", abp_id)
            stats["errors"] += 1

    return stats


async def migrate_usage_from_abp(
    source_db: AsyncIOMotorDatabase,
    target_db: AsyncIOMotorDatabase,
) -> dict[str, Any]:
    """Copy usage events from ABP's ``usage_events`` collection into ARIA's ``usage`` collection.

    ABP usage_events fields:
        source, model, input_tokens, output_tokens, task_id, session_id,
        channel, timestamp

    ARIA usage fields:
        model, source, input_tokens, output_tokens, total_tokens,
        agent_slug, conversation_id, metadata, timestamp
    """
    stats = {"examined": 0, "migrated": 0, "skipped_duplicate": 0, "errors": 0}

    source_col = source_db["usage_events"]
    target_col = target_db["usage"]

    cursor = source_col.find({})
    async for doc in cursor:
        stats["examined"] += 1
        abp_id = str(doc["_id"])

        # Idempotency check
        existing = await target_col.find_one({"metadata.abp_source_id": abp_id})
        if existing:
            stats["skipped_duplicate"] += 1
            continue

        try:
            input_tokens = doc.get("input_tokens", 0)
            output_tokens = doc.get("output_tokens", 0)

            aria_doc = {
                "model": doc.get("model", "unknown"),
                "source": doc.get("source", "abp"),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "agent_slug": None,
                "conversation_id": None,
                "metadata": {
                    "abp_source_id": abp_id,
                    "abp_task_id": doc.get("task_id", ""),
                    "abp_session_id": doc.get("session_id", ""),
                    "abp_channel": doc.get("channel", ""),
                    "migration_source": MIGRATION_SOURCE_TAG,
                    "migrated_at": datetime.now(timezone.utc).isoformat(),
                },
                "timestamp": doc.get("timestamp", datetime.now(timezone.utc)),
            }

            await target_col.insert_one(aria_doc)
            stats["migrated"] += 1
        except Exception:
            logger.exception("Failed to migrate ABP usage event %s", abp_id)
            stats["errors"] += 1

    return stats


async def run_full_migration(target_db: AsyncIOMotorDatabase) -> dict[str, Any]:
    """Run the complete ABP -> ARIA migration.

    Returns a summary dict with results from each sub-migration.
    """
    client, source_db = await _get_abp_db()
    if source_db is None:
        return {
            "status": "skipped",
            "reason": f"ABP database '{ABP_DATABASE}' not found on this MongoDB instance",
            "memories": None,
            "usage": None,
        }

    try:
        memories_result = await migrate_memories_from_abp(source_db, target_db)
        usage_result = await migrate_usage_from_abp(source_db, target_db)
    finally:
        client.close()

    return {
        "status": "completed",
        "reason": None,
        "memories": memories_result,
        "usage": usage_result,
    }


async def get_migration_status(target_db: AsyncIOMotorDatabase) -> dict[str, Any]:
    """Check whether ABP data has been migrated and return counts."""
    migrated_memories = await target_db.memories.count_documents(
        {"metadata.migration_source": MIGRATION_SOURCE_TAG}
    )
    migrated_usage = await target_db.usage.count_documents(
        {"metadata.migration_source": MIGRATION_SOURCE_TAG}
    )

    # Check if ABP database exists
    client: AsyncIOMotorClient = AsyncIOMotorClient(settings.mongodb_uri)
    try:
        db_names = await client.list_database_names()
        abp_exists = ABP_DATABASE in db_names

        abp_memories_total = 0
        abp_usage_total = 0
        if abp_exists:
            abp_db = client[ABP_DATABASE]
            abp_memories_total = await abp_db["memories"].count_documents({})
            abp_usage_total = await abp_db["usage_events"].count_documents({})
    finally:
        client.close()

    return {
        "abp_database_exists": abp_exists,
        "abp_memories_total": abp_memories_total,
        "abp_usage_total": abp_usage_total,
        "aria_migrated_memories": migrated_memories,
        "aria_migrated_usage": migrated_usage,
        "fully_migrated": (
            abp_exists
            and migrated_memories >= abp_memories_total
            and migrated_usage >= abp_usage_total
        ) if abp_exists else False,
    }
