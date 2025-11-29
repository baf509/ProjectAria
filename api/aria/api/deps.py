"""
ARIA - API Dependencies

Phase: 1
Purpose: Dependency injection for FastAPI routes

Related Spec Sections:
- Section 9.4: Dependency Injection
"""

from motor.motor_asyncio import AsyncIOMotorDatabase
from aria.db.mongodb import get_database
from aria.core.orchestrator import Orchestrator


async def get_db() -> AsyncIOMotorDatabase:
    """Get database instance."""
    return await get_database()


async def get_orchestrator(db: AsyncIOMotorDatabase = None) -> Orchestrator:
    """Get orchestrator instance."""
    if db is None:
        db = await get_database()
    return Orchestrator(db)
