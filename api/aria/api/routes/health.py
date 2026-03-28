"""
ARIA - Health Check Route

Phase: 1, 4
Purpose: Health check and LLM status endpoints

Related Spec Sections:
- Section 5.1: REST Endpoints
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from aria.api.deps import get_db
from aria.config import settings
from aria.db.models import HealthResponse
from aria.llm.manager import llm_manager

router = APIRouter()


class LLMStatusResponse(BaseModel):
    """LLM backend status response."""
    backend: str
    available: bool
    reason: str


@router.get("/health", response_model=HealthResponse)
async def health_check(
    depth: str = Query("deep", pattern="^(shallow|deep)$"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Health check with configurable depth.

    - shallow: Fast check, returns basic status without probing external services
    - deep: Verifies database, embeddings, and LLM availability
    """
    if depth == "shallow":
        return HealthResponse(
            status="healthy",
            version="0.2.0",
            database="not checked",
            timestamp=datetime.now(timezone.utc),
            embeddings="not checked",
            llm="not checked",
        )

    import httpx

    # 1. Database
    try:
        await db.command("ping")
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    # 2. Embeddings service
    embeddings_status = "unknown"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{settings.embedding_url.rstrip('/').replace('/v1', '')}/health")
            if resp.status_code == 200:
                embeddings_status = "connected"
            else:
                embeddings_status = f"http {resp.status_code}"
    except httpx.TimeoutException:
        embeddings_status = "timeout"
    except Exception:
        embeddings_status = "unreachable"

    # 3. LLM availability
    available_backends = []
    for b in ("llamacpp", "anthropic", "openai", "openrouter"):
        avail, _ = llm_manager.is_backend_available(b)
        if avail:
            available_backends.append(b)

    any_llm = len(available_backends) > 0
    llm_status = f"available ({', '.join(available_backends)})" if any_llm else "no backends configured"

    # 4. LLM telemetry summary
    telemetry = llm_manager.get_telemetry()

    # Overall status
    is_healthy = db_status == "connected"
    is_degraded = not any_llm or embeddings_status != "connected"
    if not is_healthy:
        overall = "unhealthy"
    elif is_degraded:
        overall = "degraded"
    else:
        overall = "healthy"

    return HealthResponse(
        status=overall,
        version="0.2.0",
        database=db_status,
        timestamp=datetime.now(timezone.utc),
        embeddings=embeddings_status,
        llm=llm_status,
    )


@router.get("/health/llm", response_model=list[LLMStatusResponse])
async def llm_health_check():
    """Check status of all LLM backends."""
    backends = ["llamacpp", "anthropic", "openai", "openrouter"]
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


@router.get("/health/telemetry")
async def llm_telemetry():
    """Get LLM backend telemetry (fallback counts, success/failure rates)."""
    return llm_manager.get_telemetry()
