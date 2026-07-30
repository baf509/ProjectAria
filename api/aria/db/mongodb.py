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
        # Without this, datetimes read back from Mongo are naive (no tzinfo),
        # so FastAPI/Pydantic serializes them to JSON with no offset/'Z'
        # (e.g. "2026-07-29T23:41:21.003000"). That's not valid RFC3339, and
        # the Go TUI's strict time.Time unmarshaling fails on the FIRST such
        # field -- silently failing the entire GET /conversations/{id} decode
        # on every reload. Symptom: the TUI's own optimistically-appended user
        # messages kept showing (added client-side at send time), but every
        # assistant response vanished on the next reload, because the message
        # list was never successfully re-synced from the server again. This
        # also retroactively explains the scattered `if x.tzinfo is None:
        # x = x.replace(tzinfo=timezone.utc)` guards throughout the codebase
        # (shells/service.py, shells/notifier.py, shells/selfcheck.py, etc.)
        # -- all working around this same root cause ad hoc. tz_aware=True
        # makes Motor attach UTC tzinfo to every datetime it returns, so
        # those guards become harmless no-ops instead of load-bearing.
        tz_aware=True,
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
