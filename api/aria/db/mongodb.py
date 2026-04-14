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


async def close_db():
    """Close MongoDB connection."""
    if db.client:
        db.client.close()
        logger.info("MongoDB connection closed")


async def get_database() -> AsyncIOMotorDatabase:
    """Get database instance."""
    return db.db
