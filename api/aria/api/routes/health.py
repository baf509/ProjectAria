"""
ARIA - Health Check Route

Phase: 1, 4
Purpose: Health check and LLM status endpoints

Related Spec Sections:
- Section 5.1: REST Endpoints
"""

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from aria.api.deps import get_db, get_model_server_manager
from aria.config import settings
from aria.infrastructure.model_servers import ModelServerManager
from aria.db.models import HealthResponse
from aria.llm.manager import llm_manager
from aria.memory.capabilities import retrieval_capabilities

logger = logging.getLogger(__name__)

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

    import asyncio
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
    #
    # is_backend_available() only checks that config/credentials/the SDK
    # package are present -- it never touches the network, so a backend whose
    # base_url points at a dead server (e.g. after a model-server migration
    # that forgot to update .env) was reported "available" forever. For the
    # two local backends that actually live on this box (llamacpp, agentic)
    # also require a real reachability probe, mirroring the one /health/services
    # already does. Cloud backends and context1/ridge are left as config-only
    # checks: ridge sleeps by design (see /health/services), and probing cloud
    # providers on every health check would add latency/cost for no benefit.
    _probe_urls = {"llamacpp": settings.llamacpp_url, "agentic": settings.agentic_url}

    async def _reachable(url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{url.rstrip('/')}/models")
                return resp.status_code < 500
        except Exception:
            return False

    reachability = dict(zip(
        _probe_urls.keys(),
        await asyncio.gather(*(_reachable(u) for u in _probe_urls.values())),
    )) if _probe_urls else {}

    available_backends = []
    for b in ("llamacpp", "agentic", "context1", "ridge", "anthropic", "openai", "openrouter"):
        avail, _ = llm_manager.is_backend_available(b)
        if avail and b in reachability and not reachability[b]:
            avail = False
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
    backends = ["llamacpp", "agentic", "context1", "ridge", "anthropic", "openai", "openrouter"]
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
async def services_health(
    db: AsyncIOMotorDatabase = Depends(get_db),
    model_servers: ModelServerManager = Depends(get_model_server_manager),
):
    """Concurrently probe every backing service and report per-service health.

    Powers the TUI/web health page: mongod, mongot, the three local llama.cpp
    servers, embeddings, tts, and stt.
    """
    import asyncio
    import time
    import httpx

    def _base(url: str) -> str:
        return url.rstrip("/").replace("/v1", "")

    async def http_ping(
        name: str,
        url: str,
        headers: dict | None = None,
        *,
        timeout: float = 4.0,
    ) -> dict:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers or {})
            # 4xx auth failures mean the credential is wrong — that is *not*
            # healthy, even though the server answered.
            return {
                "name": name,
                "ok": resp.status_code < 500 and resp.status_code not in (401, 403),
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

    async def _disabled_capability(name: str) -> dict:
        """Awaitable stand-in so a disabled capability can sit in the task list."""
        cap = retrieval_capabilities.status().get(name, {})
        reason = cap.get("reason") or "switched off"
        return {
            "name": name,
            "ok": True,
            "latency_ms": 0,
            "detail": f"capability disabled ({reason})",
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
        #
        # A capability switched OFF is not a degraded one — same rule as a
        # deliberately-stopped model server, and the reason the switch exists:
        # without this, turning mongot off to free the box would page the
        # Hermes alert-triage cron every 10 minutes about a decision the
        # operator made on purpose.
        if not retrieval_capabilities.search_enabled:
            return {
                "name": "mongot",
                "ok": True,
                "latency_ms": 0,
                "detail": "search capability disabled (not probed)",
            }
        t0 = time.monotonic()
        try:
            cur = db.memories.aggregate([{"$listSearchIndexes": {}}])
            await cur.to_list(length=1)
            return {"name": "mongot", "ok": True, "latency_ms": round((time.monotonic() - t0) * 1000), "detail": "search indexes ok"}
        except Exception as e:
            return {"name": "mongot", "ok": False, "latency_ms": round((time.monotonic() - t0) * 1000), "detail": str(e)[:80]}

    # Only services that are actually meant to be up are probed. A disabled or
    # unconfigured backend is not a degraded one, so it is left out entirely
    # rather than counted against `healthy`.
    # A model server that is *deliberately stopped* is not a degraded one. The
    # big on-box servers are mutually RAM-exclusive, so at most one of them can
    # be up at a time and the rest are stopped ON PURPOSE — probing them
    # unconditionally paints the health screen red forever and, worse, feeds the
    # Hermes alert-triage cron incidents that have no fix. Same reasoning (and
    # same wording) as the `pool_enabled` skip in shells/selfcheck.py.
    # Consult the registry so the probe can tell "stopped on purpose" apart from
    # "should be up and isn't"; a registry failure just degrades to probing.
    stopped_on_purpose: dict[int, str] = {}
    try:
        for s in await model_servers.status(db):
            if s.get("onbox") and s.get("port") and s.get("state") != "running":
                stopped_on_purpose[int(s["port"])] = s["slug"]
    except Exception as e:  # registry is advisory here, never fatal to health
        logger.debug("services_health: model-server registry unavailable: %s", e)

    # The NON-LLM half of the same question, from the sibling registry. The two
    # are kept separate on purpose: a stopped model server is normal (they are
    # mutually RAM-exclusive), whereas a stopped mongod is always an incident.
    # `expected_state` is what carries that difference — an `on_demand` service
    # may report healthy while stopped; an `always_up` one may not, ever.
    #
    # This closed a real hole: aria-stt had been EXITED for 7 days (as of
    # 2026-08-07) while this endpoint probed it unconditionally, so it was
    # silently counted unhealthy every tick with no way to say "that's fine".
    svc_expected: dict[int, dict] = {}
    svc_all: list[dict] = []
    try:
        from aria.infrastructure.services import get_service_manager

        svc_all = await get_service_manager().status(db)
        for s in svc_all:
            if s.get("port"):
                svc_expected[int(s["port"])] = s
    except Exception as e:  # advisory, exactly like the model-server lookup
        logger.debug("services_health: service registry unavailable: %s", e)

    def _port_of(url: str) -> int | None:
        try:
            return urlparse(url).port
        except ValueError:
            return None

    async def llm_ping(name: str, url: str) -> dict:
        """Probe a local-LLM slot, tolerating a deliberately-stopped server.

        Sends the ARIA key because llamacpp_url now points at this app's own
        /llm/v1 passthrough (see config.llamacpp_url), which sits behind
        api_key_middleware — without it the probe 401s and reads as unhealthy.
        A real llama.cpp server ignores the extra header.
        """
        slug = stopped_on_purpose.get(_port_of(url) or -1)
        if slug:
            return {
                "name": name,
                "ok": True,
                "latency_ms": 0,
                "detail": f"{slug} stopped (start on demand)",
            }
        headers = {"X-API-Key": settings.api_key} if settings.api_key else None
        # The gateway catalog resolves observed model state across the remote
        # inference plane. Four seconds was below normal warm-path latency and
        # produced false incidents; twelve still fails quickly enough for an
        # operator health surface while covering the measured control-plane
        # path.
        return await http_ping(
            name,
            f"{url.rstrip('/')}/models",
            headers=headers,
            timeout=12.0,
        )

    async def svc_ping(name: str, url: str) -> dict:
        """Probe a non-LLM service, honouring its registry `expected_state`.

        An `on_demand` service that is simply stopped is reported healthy with
        a reason, rather than as a failure nobody can act on. An `always_up`
        one is probed normally — if it is down, that must surface.
        """
        entry = svc_expected.get(_port_of(url) or -1)
        if (
            entry
            and entry.get("expected_state") == "on_demand"
            and entry.get("state") != "running"
        ):
            return {
                "name": name,
                "ok": True,
                "latency_ms": 0,
                "detail": f"{entry['slug']} stopped (on demand)",
            }
        return await http_ping(name, url)

    tasks = [
        mongo_ping(),
        mongot_ping(),
        # Labels describe the ROLE, not a specific model. These probe whatever
        # llamacpp_url / agentic_url currently point at. Since 2026-08-05
        # llamacpp_url is ARIA's own /llm/v1 passthrough rather than a fixed
        # model port, so "orchestrator" follows the resident model automatically;
        # agentic_url still names the on-demand local coding server (:8105).
        # They were previously labelled "qwen-chat" / "qwen-agentic", which made
        # the TUI health screen and every consumer of this endpoint report a
        # model that has not run here in months.
        llm_ping("local-llm (orchestrator)", settings.llamacpp_url),
        llm_ping("local-llm (coding)", settings.agentic_url),
        # NOTE: ridge (:8092 -> Ridge's RTX 3090) is deliberately NOT probed here.
        # Ridge sleeps when idle, so a probe would either report it DOWN when it
        # is merely asleep, or send a Wake-on-LAN on every health tick and keep a
        # gaming PC awake 24/7. Its liveness is the proxy's job, not this list's.
        # Routed through svc_ping so a deliberately-stopped on_demand service
        # (stt, today) reads as "stopped on demand" rather than as a failure.
        # embeddings and tts are always_up in the registry, so this is a no-op
        # for them — they still get a real probe and still go red when down.
        # Same rule as mongot above: a disabled capability is reported as such
        # rather than probed and counted unhealthy.
        (
            svc_ping("embeddings", f"{_base(settings.embedding_url)}/health")
            if retrieval_capabilities.embeddings_enabled
            else _disabled_capability("embeddings")
        ),
        svc_ping("tts", f"{_base(settings.tts_url)}/health"),
        svc_ping("stt", f"{_base(settings.stt_url)}/health"),
    ]
    if settings.context1_enabled:
        tasks.append(http_ping("context-1", f"{settings.context1_url.rstrip('/')}/models"))
    results = list(await asyncio.gather(*tasks))

    # Registry-driven additions: the always_up services with no HTTP surface to
    # probe (signal-cli, hermes-gateway, samba, the tmux owner, ...). Before
    # this, nothing here noticed if signal-cli died — which silently takes out
    # alert triage and the Signal→Linear capture path. Process state IS the
    # health signal for these; there is nothing to GET.
    #
    # Ports already probed above are skipped so a service is never double-
    # counted, and on_demand services are omitted entirely rather than padding
    # the healthy/total ratio with things that are meant to be down.
    probed_ports = {
        _port_of(u)
        for u in (settings.embedding_url, settings.tts_url, settings.stt_url)
    } - {None}
    for s in svc_all:
        if s.get("state") == "not_applicable":
            continue
        if s.get("expected_state") != "always_up":
            continue
        if s.get("port") in probed_ports:
            continue
        # mongod/mongot have dedicated probes above that test real reachability
        # rather than container state; don't shadow them with a weaker signal.
        if s["slug"] in ("shared-mongod", "shared-mongot"):
            continue
        results.append(
            {
                "name": s["slug"],
                "ok": bool(s["healthy"]),
                "latency_ms": 0,
                "detail": f"{s['state']} ({s.get('unit') or s.get('container') or 'process'})",
            }
        )

    healthy = sum(1 for r in results if r["ok"])
    return {"services": results, "healthy": healthy, "total": len(results)}


@router.get("/health/telemetry")
async def llm_telemetry():
    """Get LLM backend telemetry (fallback counts, success/failure rates)."""
    return llm_manager.get_telemetry()
