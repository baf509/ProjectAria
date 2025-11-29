"""
ARIA - MongoDB Connection

Phase: 1
Purpose: MongoDB connection management using motor (async driver)

Related Spec Sections:
- Section 4: Data Models
- Section 11.1: Docker Configuration
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from aria.config import settings


class Database:
    """MongoDB database connection manager."""

    client: AsyncIOMotorClient = None
    db: AsyncIOMotorDatabase = None


db = Database()


async def connect_db():
    """Connect to MongoDB."""
    print(f"Connecting to MongoDB at {settings.mongodb_uri}")
    db.client = AsyncIOMotorClient(settings.mongodb_uri)
    db.db = db.client[settings.mongodb_database]
    print(f"Connected to database: {settings.mongodb_database}")


async def close_db():
    """Close MongoDB connection."""
    if db.client:
        db.client.close()
        print("MongoDB connection closed")


async def get_database() -> AsyncIOMotorDatabase:
    """Get database instance."""
    return db.db
