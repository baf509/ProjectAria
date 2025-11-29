"""
ARIA - Health Check Route

Phase: 1
Purpose: Health check endpoint

Related Spec Sections:
- Section 5.1: REST Endpoints
"""

from datetime import datetime
from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.api.deps import get_db
from aria.db.models import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Health check endpoint."""
    # Test database connection
    try:
        await db.command("ping")
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    return HealthResponse(
        status="healthy" if db_status == "connected" else "unhealthy",
        version="0.2.0",
        database=db_status,
        timestamp=datetime.utcnow(),
    )
