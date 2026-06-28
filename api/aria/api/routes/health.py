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
    for b in ("llamacpp", "agentic", "context1", "anthropic", "openai", "openrouter", "fireworks"):
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
    backends = ["llamacpp", "agentic", "context1", "anthropic", "openai", "openrouter", "fireworks"]
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


@router.get("/health/services")
async def services_health(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Concurrently probe every backing service and report per-service health.

    Powers the TUI/web health page: mongod, mongot, the three local llama.cpp
    servers, embeddings, tts, stt, and Fireworks reachability.
    """
    import asyncio
    import time
    import httpx

    def _base(url: str) -> str:
        return url.rstrip("/").replace("/v1", "")

    async def http_ping(name: str, url: str, headers: dict | None = None) -> dict:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(url, headers=headers or {})
            return {
                "name": name,
                "ok": resp.status_code < 500,
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "detail": f"http {resp.status_code}",
            }
        except Exception as e:
            return {
                "name": name,
                "ok": False,
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "detail": type(e).__name__,
            }

    async def mongo_ping() -> dict:
        t0 = time.monotonic()
        try:
            await db.command("ping")
            return {"name": "mongod", "ok": True, "latency_ms": round((time.monotonic() - t0) * 1000), "detail": "ping ok"}
        except Exception as e:
            return {"name": "mongod", "ok": False, "latency_ms": round((time.monotonic() - t0) * 1000), "detail": str(e)[:80]}

    async def mongot_ping() -> dict:
        # mongot isn't exposed on the host; verify it via a search-index list
        # (which is served by mongot through mongod).
        t0 = time.monotonic()
        try:
            cur = db.memories.aggregate([{"$listSearchIndexes": {}}])
            await cur.to_list(length=1)
            return {"name": "mongot", "ok": True, "latency_ms": round((time.monotonic() - t0) * 1000), "detail": "search indexes ok"}
        except Exception as e:
            return {"name": "mongot", "ok": False, "latency_ms": round((time.monotonic() - t0) * 1000), "detail": str(e)[:80]}

    tasks = [
        mongo_ping(),
        mongot_ping(),
        http_ping("qwen-chat", f"{settings.llamacpp_url.rstrip('/')}/models"),
        http_ping("qwen-agentic", f"{settings.agentic_url.rstrip('/')}/models"),
        http_ping("context-1", f"{settings.context1_url.rstrip('/')}/models"),
        http_ping("embeddings", f"{_base(settings.embedding_url)}/health"),
        http_ping("tts", f"{_base(settings.tts_url)}/health"),
        http_ping("stt", f"{_base(settings.stt_url)}/health"),
    ]
    if settings.fireworks_api_key:
        tasks.append(http_ping(
            "fireworks",
            f"{settings.fireworks_base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {settings.fireworks_api_key}"},
        ))
    results = list(await asyncio.gather(*tasks))
    if not settings.fireworks_api_key:
        results.append({"name": "fireworks", "ok": False, "latency_ms": 0, "detail": "not configured"})

    healthy = sum(1 for r in results if r["ok"])
    return {"services": results, "healthy": healthy, "total": len(results)}


@router.get("/health/telemetry")
async def llm_telemetry():
    """Get LLM backend telemetry (fallback counts, success/failure rates)."""
    return llm_manager.get_telemetry()
