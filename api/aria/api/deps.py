"""
ARIA - API Dependencies

Phase: 1
Purpose: Dependency injection for FastAPI routes

Related Spec Sections:
- Section 9.4: Dependency Injection
"""

from typing import Annotated
from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorDatabase
from aria.db.mongodb import get_database
from aria.core.orchestrator import Orchestrator


async def get_db() -> AsyncIOMotorDatabase:
    """Get database instance."""
    return await get_database()


async def get_orchestrator(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)]
) -> Orchestrator:
    """Get orchestrator instance."""
    return Orchestrator(db)
