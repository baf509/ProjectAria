"""
ARIA - Health Check Route

Phase: 1, 4
Purpose: Health check and LLM status endpoints

Related Spec Sections:
- Section 5.1: REST Endpoints
"""

from datetime import datetime
from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from aria.api.deps import get_db
from aria.db.models import HealthResponse
from aria.llm.manager import llm_manager

router = APIRouter()


class LLMStatusResponse(BaseModel):
    """LLM backend status response."""
    backend: str
    available: bool
    reason: str


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


@router.get("/health/llm", response_model=list[LLMStatusResponse])
async def llm_health_check():
    """Check status of all LLM backends."""
    backends = ["ollama", "anthropic", "openai"]
    statuses = []

    for backend in backends:
        available, reason = llm_manager.is_backend_available(backend)
        statuses.append(
            LLMStatusResponse(
                backend=backend,
                available=available,
                reason=reason,
            )
        )

    return statuses
