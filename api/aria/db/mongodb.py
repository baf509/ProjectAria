"""
ARIA - MongoDB Connection

Phase: 1
Purpose: MongoDB connection management using motor (async driver)

Related Spec Sections:
- Section 4: Data Models
- Section 11.1: Docker Configuration
"""

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from aria.config import settings

logger = logging.getLogger(__name__)


class Database:
    """MongoDB database connection manager."""

    client: AsyncIOMotorClient = None
    db: AsyncIOMotorDatabase = None


db = Database()


async def connect_db():
    """Connect to MongoDB with pool configuration and connectivity verification."""
    logger.info("Connecting to MongoDB at %s", settings.mongodb_uri)
    db.client = AsyncIOMotorClient(
        settings.mongodb_uri,
        maxPoolSize=settings.mongodb_max_pool_size,
        minPoolSize=settings.mongodb_min_pool_size,
        maxIdleTimeMS=30000,
    )
    db.db = db.client[settings.mongodb_database]

    # Verify connectivity at startup — fail fast with a clear error
    try:
        await db.client.admin.command("ping")
    except Exception as e:
        db.client.close()
        db.client = None
        db.db = None
        raise RuntimeError(f"MongoDB connection failed: {e}") from e

    logger.info("Connected to database: %s", settings.mongodb_database)


async def ensure_indexes():
    """Create or verify required indexes for ARIA collections."""
    database = db.db
    if database is None:
        logger.warning("Cannot ensure indexes: database not connected")
        return

    try:
        # Conversations indexes
        await database.conversations.create_index(
            [("status", 1), ("updated_at", -1)],
            name="status_updated",
        )
        await database.conversations.create_index(
            [("agent_id", 1)],
            name="agent_id",
        )

        # Memories indexes
        await database.memories.create_index(
            [("status", 1), ("created_at", -1)],
            name="status_created",
        )
        await database.memories.create_index(
            [("status", 1), ("categories", 1)],
            name="status_categories",
        )
        await database.memories.create_index(
            [("last_accessed_at", 1)],
            name="last_accessed",
        )

        # Usage indexes
        await database.usage.create_index(
            [("created_at", -1)],
            name="usage_created",
        )

        # Dream journal indexes
        await database.dream_journal.create_index(
            [("created_at", -1)],
            name="dream_journal_created",
        )
        await database.dream_soul_proposals.create_index(
            [("status", 1), ("created_at", -1)],
            name="soul_proposals_status_created",
        )

        # Tool audit indexes
        await database.tool_audit.create_index(
            [("created_at", -1)],
            name="audit_created",
        )
        await database.tool_audit.create_index(
            [("tool_name", 1), ("created_at", -1)],
            name="audit_tool_created",
        )

        logger.info("Database indexes verified")
    except Exception as e:
        logger.warning("Failed to ensure indexes (non-fatal): %s", e)


async def close_db():
    """Close MongoDB connection."""
    if db.client:
        db.client.close()
        logger.info("MongoDB connection closed")


async def get_database() -> AsyncIOMotorDatabase:
    """Get database instance."""
    return db.db
