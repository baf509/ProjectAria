"""OpenAI-compatible passthrough to whichever local model is currently resident.

Why this exists: consumers used to hardcode a port for "the local model"
(`LLAMACPP_URL`), and every time the resident model changed — qwen -> laguna ->
DS4 — every consumer silently pointed at a dead port. `endpoints.env` carries a
comment about exactly that failure lasting weeks.

ARIA already owns the model-server control plane: it knows what is installed,
what is running, and how to reach it. So it is the right place to answer "give
me the local model" without the caller naming a port.

    LLAMACPP_URL=http://localhost:8200/llm/v1

Mounted at the app root, NOT under /api/v1, so the path ends in a bare `/v1`
and any stock OpenAI client works unmodified.

Selection, in precedence order (more than one server CAN be resident — gemma is
CPU-only and coexists with anything, and the chadrock+qwen split is a deliberate
~89 GiB pair, so "the local model" is not always unambiguous):

  1. The request's own `model` field, when it names a running server (by slug or
     by its .gguf filename). llama.cpp ignores unknown `model` values entirely —
     verified against gemma, which answers happily for a made-up name — so the
     field is free for ARIA to use as a routing selector. This is what makes
     Hermes's `/model <slug>` pick between loaded models with no restart.
     Naming a server that ARIA knows but that is *stopped* is an error (503),
     not a silent downgrade; naming something unknown (`gpt-4`) falls through
     to auto, since arbitrary model strings are normal for OpenAI clients.
  2. A pin set in ARIA (`PUT /api/v1/infrastructure/llm-route`) — the
     deterministic answer when several servers are up. A pin whose server is
     not running degrades to auto rather than failing: a stale pin must never
     take every consumer down, which is the exact drift this module exists to
     prevent.
  3. Auto: the largest measured `resident_gib` among running on-box servers —
     the "main" model by construction, since the big ones are mutually
     exclusive. Gemma (8 GiB) therefore serves when nothing large is resident
     and steps aside the moment a big model starts.

Callers that want a *specific* model can still address its port directly.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.api.deps import get_db, get_model_server_manager
from aria.config import settings
from aria.db.usage import UsageRepo
from aria.infrastructure.model_servers import ModelServerManager
from aria.infrastructure.llm_route import (
    backend_model_id as _backend_model_id,
    base_url_for,
    is_servable,
    read_pin,
    select,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm/v1", tags=["llm-proxy"])

# Same proxy, but every request gets one system line naming the model actually
# serving it. Why a SEPARATE route rather than a flag on /llm/v1:
#
#   /llm/v1 is `LLAMACPP_URL` — it feeds evalstack and the benchmark routes as
#   well as the agents. Silently prepending a system message there would change
#   the prompt under every benchmark and quietly corrupt results. So the
#   verbatim path stays verbatim, and anything that wants self-knowledge opts in
#   by pointing here instead.
#
# The problem this solves: consumers configure the synthetic id `aria-resident`,
# which is a routing alias, not a model. Asked what model they are, they answer
# from config — "aria-resident on a custom provider" — which describes the
# plumbing and names nothing. A tool call can fetch the truth but only if the
# model chooses to call it; this puts it in the context unconditionally.
#
# Costs no prompt cache: the injected line changes only when the resident model
# changes, and that swap restarts the backend and voids its cache anyway.
identified_router = APIRouter(prefix="/llm/v1-identified", tags=["llm-proxy"])

# Generation can be slow on a 2.58 BPW MoE at long context: DS4 decodes at
# ~11 tok/s at 32K, so a 2k-token answer is minutes. Connect fast, read slow.
_TIMEOUT = httpx.Timeout(connect=5.0, read=1800.0, write=60.0, pool=5.0)

# One client for the process lifetime. Every request used to open a fresh
# httpx.AsyncClient — a new TCP handshake to the local model server on every
# LLM call, no keep-alive. httpx pools connections per host, so a shared
# client keeps the sockets warm across requests; a restarted backend is
# reconnected transparently (the connect timeout bounds the wait).
_shared_client: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _shared_client


async def close_client() -> None:
    """Close the shared client (called from the app lifespan shutdown)."""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None

# Hop-by-hop headers must not be forwarded (RFC 7230 6.1), plus the ones that
# would misdescribe the proxied body.
_STRIP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "accept-encoding", "x-aria-caller",
}

_CALLER_SAFE = re.compile(r"[^a-zA-Z0-9_.:@/-]+")

# Caller labels are QoS hints, never authorization. A forged label can affect
# queue order but cannot grant a scope or bypass the API key middleware.
_BACKGROUND_CALLER_MARKERS = (
    "background", "benchmark", "eval", "cron", "worker", "steward",
    "maintenance", "probe", "auxiliary", "compaction",
)


def _caller_priority(caller: str) -> int:
    """0=human conversation, 1=foreground coding, 2=background work."""
    normalized = caller.casefold()
    if normalized == "hermes" or normalized.startswith("hermes-"):
        return 0
    if any(marker in normalized for marker in _BACKGROUND_CALLER_MARKERS):
        return 2
    return 1


@dataclass(frozen=True)
class _AdmissionStats:
    controlled: bool
    priority: int
    queue_wait_ms: float = 0.0
    queue_depth_at_arrival: int = 0


@dataclass
class _QueuedRequest:
    future: asyncio.Future[None]
    priority: int
    sequence: int
    enqueued_at: float


class _PriorityAdmission:
    """Cancellation-safe, aging priority queue for one physical model slot."""

    def __init__(
        self,
        aging_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._aging_seconds = max(0.001, float(aging_seconds))
        self._clock = clock
        self._lock = asyncio.Lock()
        self._active = False
        self._waiters: list[_QueuedRequest] = []
        self._sequence = 0

    def _effective_priority(self, item: _QueuedRequest, now: float) -> int:
        tiers = int(max(0.0, now - item.enqueued_at) / self._aging_seconds)
        return max(0, item.priority - tiers)

    async def acquire(self, priority: int) -> _AdmissionStats:
        enqueued_at = self._clock()
        async with self._lock:
            self._waiters = [item for item in self._waiters if not item.future.done()]
            depth = len(self._waiters) + (1 if self._active else 0)
            if not self._active and not self._waiters:
                self._active = True
                return _AdmissionStats(True, priority, 0.0, depth)
            future = asyncio.get_running_loop().create_future()
            item = _QueuedRequest(future, priority, self._sequence, enqueued_at)
            self._sequence += 1
            self._waiters.append(item)

        try:
            await future
        except BaseException:
            # Cancellation can race with release() granting this request. If it
            # was granted, pass the slot onward; if it was still queued, remove
            # it so a disconnected client cannot consume future capacity.
            granted = future.done() and not future.cancelled()
            if granted:
                await asyncio.shield(self.release())
            else:
                async with self._lock:
                    self._waiters = [queued for queued in self._waiters if queued is not item]
            raise
        return _AdmissionStats(
            True,
            priority,
            round((self._clock() - enqueued_at) * 1000, 2),
            depth,
        )

    async def release(self) -> None:
        async with self._lock:
            self._active = False
            while self._waiters:
                now = self._clock()
                item = min(
                    self._waiters,
                    key=lambda queued: (
                        self._effective_priority(queued, now), queued.sequence
                    ),
                )
                self._waiters.remove(item)
                if item.future.done():
                    continue
                self._active = True
                try:
                    item.future.set_result(None)
                except asyncio.InvalidStateError:
                    self._active = False
                    continue
                break

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            pending = [item for item in self._waiters if not item.future.done()]
            counts = {"interactive": 0, "foreground": 0, "background": 0}
            labels = ("interactive", "foreground", "background")
            for item in pending:
                counts[labels[min(2, max(0, item.priority))]] += 1
            return {
                "active": self._active,
                "queued": len(pending),
                "queued_by_priority": counts,
            }


_admissions: dict[str, _PriorityAdmission] = {}


def _route_slots(route: "_Route") -> Optional[int]:
    for server in route.servers:
        if server.get("slug") != route.slug:
            continue
        value = server.get("slots")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _admission_for(route: "_Route") -> Optional[_PriorityAdmission]:
    if (
        not settings.llm_proxy_admission_enabled
        or _route_slots(route) != 1
        or not route.base_url
    ):
        return None
    admission = _admissions.get(route.base_url)
    if admission is None:
        admission = _PriorityAdmission(settings.llm_proxy_queue_aging_seconds)
        _admissions[route.base_url] = admission
    return admission


@asynccontextmanager
async def _admit(route: "_Route", caller: str) -> AsyncIterator[_AdmissionStats]:
    priority = _caller_priority(caller)
    admission = _admission_for(route)
    if admission is None:
        yield _AdmissionStats(False, priority)
        return
    stats = await admission.acquire(priority)
    try:
        yield stats
    finally:
        # A cancelled stream still must hand the only model slot onward.
        await asyncio.shield(admission.release())


async def _admission_snapshot(route: "_Route") -> dict[str, Any]:
    slots = _route_slots(route)
    admission = _admission_for(route)
    state = await admission.snapshot() if admission is not None else {
        "active": False,
        "queued": 0,
        "queued_by_priority": {
            "interactive": 0, "foreground": 0, "background": 0,
        },
    }
    return {
        "controlled": admission is not None,
        "slots": slots,
        "aging_seconds": (
            settings.llm_proxy_queue_aging_seconds if admission is not None else None
        ),
        **state,
    }


def _gateway_caller(request: Request) -> tuple[str, str, str]:
    """Return a bounded attribution label plus diagnostic network identity.

    Known clients set X-Aria-Caller. It is attribution, not authorization, so
    it is deliberately never used for access control. Unknown clients still
    get a stable caller based on their peer and user-agent instead of dropping
    an unqueryable usage row.
    """
    host = request.client.host if request.client else "unknown"
    user_agent = (request.headers.get("user-agent") or "unknown")[:160]
    declared = (request.headers.get("x-aria-caller") or "").strip()[:80]
    if declared:
        caller = _CALLER_SAFE.sub("_", declared).strip("_") or "unknown"
    else:
        agent = _CALLER_SAFE.sub("_", user_agent.split(" ", 1)[0]).strip("_")
        caller = f"{host}:{agent or 'unknown'}"
    return caller[:120], host[:120], user_agent


def _usage_counts(payload: Optional[dict]) -> tuple[int, int, int, int]:
    """Fresh input, output, cache-read, and raw prompt token counts.

    OpenAI's prompt_tokens includes cached input. UsageRepo's cache-hit formula
    expects input_tokens to mean fresh input, so subtract cached tokens once.
    llama.cpp also exposes timings.cache_n as a fallback for older responses.
    """
    payload = payload if isinstance(payload, dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    details = (
        usage.get("prompt_tokens_details")
        if isinstance(usage.get("prompt_tokens_details"), dict)
        else {}
    )
    timings = payload.get("timings") if isinstance(payload.get("timings"), dict) else {}

    def count(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    prompt = count(usage.get("prompt_tokens")) or count(timings.get("prompt_n"))
    output = count(usage.get("completion_tokens")) or count(timings.get("predicted_n"))
    cached = count(details.get("cached_tokens")) or count(timings.get("cache_n"))
    cached = min(cached, prompt) if prompt else cached
    return max(0, prompt - cached), output, cached, prompt


async def _record_gateway_usage(
    db: AsyncIOMotorDatabase,
    *,
    request: Request,
    route: Optional["_Route"],
    requested_model: Optional[str],
    backend_model_id: Optional[str],
    path: str,
    identify: bool,
    started: float,
    status_code: int,
    response_payload: Optional[dict] = None,
    streamed: bool = False,
    error: Optional[str] = None,
    admission: Optional[_AdmissionStats] = None,
) -> None:
    """Persist one gateway request without ever storing prompt/response text.

    Accounting is best-effort: a Mongo outage must not replace a successful
    model response with a gateway error.
    """
    try:
        caller, client_host, user_agent = _gateway_caller(request)
        fresh_input, output, cache_read, prompt = _usage_counts(response_payload)
        slug = route.slug if route and route.slug else None
        await UsageRepo(db).record(
            model=slug or backend_model_id or requested_model or "unknown",
            source="llm-gateway",
            backend="local",
            caller=caller,
            input_tokens=fresh_input,
            output_tokens=output,
            cache_read_tokens=cache_read,
            metadata={
                "path": path,
                "identified": identify,
                "streamed": streamed,
                "status_code": status_code,
                "outcome": "ok" if 200 <= status_code < 400 and not error else "error",
                "error": error,
                "requested_model": requested_model,
                "resolved_slug": slug,
                "backend_model_id": backend_model_id,
                "route_reason": route.reason if route else None,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "admission_controlled": bool(admission and admission.controlled),
                "queue_priority": admission.priority if admission else None,
                "queue_wait_ms": admission.queue_wait_ms if admission else 0.0,
                "queue_depth_at_arrival": (
                    admission.queue_depth_at_arrival if admission else 0
                ),
                "prompt_tokens_reported": prompt,
                "client_host": client_host,
                "user_agent": user_agent,
            },
        )
    except Exception:
        logger.warning("llm-proxy: failed to persist gateway usage", exc_info=True)


class _StreamUsage:
    """Incrementally retain only SSE usage metadata, never generated text."""

    def __init__(self) -> None:
        self._buffer = b""
        self.payload: dict = {}

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        lines = self._buffer.split(b"\n")
        self._buffer = lines.pop()
        # A malformed/non-SSE backend must not make us retain an unbounded body.
        if len(self._buffer) > 1024 * 1024:
            self._buffer = self._buffer[-1024 * 1024:]
        for line in lines:
            self._line(line)

    def finish(self) -> None:
        if self._buffer:
            self._line(self._buffer)
        self._buffer = b""

    def _line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        raw = line[5:].strip()
        if not raw or raw == b"[DONE]":
            return
        try:
            item = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(item, dict):
            return
        if isinstance(item.get("usage"), dict):
            self.payload["usage"] = item["usage"]
        if isinstance(item.get("timings"), dict):
            self.payload["timings"] = item["timings"]


class _Route:
    """Resolved answer to 'who serves this request'."""

    __slots__ = ("slug", "base_url", "reason", "servers", "unavailable")

    def __init__(self, slug, base_url, reason, servers, unavailable=False):
        self.slug = slug
        self.base_url = base_url
        self.reason = reason
        self.servers = servers
        self.unavailable = unavailable


async def _pick_backend(
    manager: ModelServerManager,
    db: AsyncIOMotorDatabase,
    requested: Optional[str] = None,
) -> _Route:
    """Resolve request-model > operator pin > largest resident. See module docstring.

    Uses the light `running_summary()` — two subprocesses, not the full
    status()'s ~70-80 — because routing only needs which servers are running
    and their relative footprint. The full view is still available to the
    display endpoints, which poll rather than sit on the request path.
    """
    servers = await _running_summary_cached(manager, db)
    pin = await read_pin(db)
    chosen, reason, unavailable = select(servers, requested=requested, pin=pin)
    if chosen is None:
        return _Route(None, None, reason, servers, unavailable)
    return _Route(chosen.get("slug"), base_url_for(chosen), reason, servers)


# In forward mode `running_summary()` probes every forwarded endpoint with a
# full HTTP /health round trip through the Corsair SSH tunnel (each bounded by
# sub-second timeouts that stack when a tunnel is stale), so answering every
# request from a fresh summary made the gateway 2-4s slower than the backend
# it fronts. Cache the summary briefly instead: routing only ranks footprint
# magnitudes and reads start/stop states, which change rarely. The autostart
# path forces its own fresh, uncached status() and drops this cache.
_SUMMARY_TTL_SECONDS = 3.0
_summary_cache: Optional[tuple[float, list[dict]]] = None


def _drop_summary_cache() -> None:
    global _summary_cache
    _summary_cache = None


async def _running_summary_cached(
    manager: ModelServerManager,
    db: AsyncIOMotorDatabase,
) -> list[dict]:
    global _summary_cache
    now = time.monotonic()
    if _summary_cache is not None and now - _summary_cache[0] < _SUMMARY_TTL_SECONDS:
        return _summary_cache[1]
    servers = await manager.running_summary(db)
    _summary_cache = (now, servers)
    return servers


def _unavailable(route: _Route) -> HTTPException:
    startable = sorted(
        s["slug"] for s in route.servers
        if s.get("onbox") and s.get("startable") and s.get("state") != "running"
    )
    if route.unavailable:
        # The caller named a specific server that ARIA knows but isn't running.
        # Answering with a different model would be worse than failing.
        return HTTPException(
            status_code=503,
            detail={
                "error": route.reason,
                "hint": "start it via ARIA, or omit `model` to use whichever is resident",
                "running": sorted(s["slug"] for s in route.servers if is_servable(s)),
                "startable": startable,
            },
        )
    return HTTPException(
        status_code=503,
        detail={
            "error": "no local model server is currently running",
            "hint": "start one via ARIA (POST /api/v1/infrastructure/model-servers/{slug}/start)",
            "startable": startable,
        },
    )


async def _context_length(base: str) -> Optional[int]:
    """Ask a backend for its loaded model's context window.

    Consumers configure a static context_length against this proxy, so the
    honest value has to come from the backend that will actually serve — a
    guess here is how a caller ends up overflowing a smaller resident model.
    """
    try:
        resp = await _client().get(f"{base}/models", timeout=5.0)
        for entry in (resp.json() or {}).get("data") or []:
            n_ctx = (entry.get("meta") or {}).get("n_ctx")
            if isinstance(n_ctx, int) and n_ctx > 0:
                return n_ctx
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return None
    return None


def _forward_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _STRIP}


@router.get("/models")
async def list_models(
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Any:
    """OpenAI /v1/models — every server that is loaded right now, plus `aria-resident`.

    Deliberately a catalogue rather than a passthrough of one backend's answer:
    more than one model can be resident, and a consumer can only *pick* between
    them (Hermes's `/model`) if this endpoint lists them. The synthetic
    `aria-resident` entry is the auto route — the id to configure when you want
    to follow whatever ARIA has running rather than name a model.
    """
    route = await _pick_backend(manager, db)
    if not route.base_url:
        raise _unavailable(route)

    loaded = [s for s in route.servers if is_servable(s)]
    ctxs = await asyncio.gather(
        *(_context_length(base_url_for(s) or "") for s in loaded),
        return_exceptions=True,
    )

    data: list[dict] = []
    smallest_ctx: Optional[int] = None
    for server, n_ctx in zip(loaded, ctxs):
        n_ctx = n_ctx if isinstance(n_ctx, int) else None
        if n_ctx and (smallest_ctx is None or n_ctx < smallest_ctx):
            smallest_ctx = n_ctx
        data.append({
            "id": server["slug"],
            "object": "model",
            "owned_by": "aria",
            "aliases": [],
            "meta": {
                "n_ctx": n_ctx,
                "context_length": n_ctx,
                "resident_gib": server.get("resident_gib_estimate"),
                "model_file": server.get("model_file"),
                "backend_device": server.get("backend_device"),
                "base_url": base_url_for(server),
                "serving": server["slug"] == route.slug,
            },
        })

    # The auto entry advertises the SMALLEST resident context, not the current
    # one: it can be served by any loaded model, so promising more than the
    # smallest would overflow the moment the auto pick moves.
    # `name`/`description` name the model this alias currently resolves to, so a
    # client that renders the catalogue shows the real model rather than the
    # routing alias. A consumer asked "what model are you?" otherwise reports
    # its config — "aria-resident on a custom provider" — which describes the
    # plumbing, not the model.
    resolved = route.slug or "nothing"
    data.insert(0, {
        "id": "aria-resident",
        "object": "model",
        "owned_by": "aria",
        "name": f"aria-resident → {resolved}",
        "description": (
            f"Routing alias, not a model. Currently resolves to {resolved}"
            f" ({route.reason}). Ask /api/v1/infrastructure/llm-route for the"
            f" loaded model id as the backend itself reports it."
        ),
        "aliases": sorted(a for a in ("auto", "aria") if a),
        "meta": {
            "n_ctx": smallest_ctx,
            "context_length": smallest_ctx,
            "serving": True,
            "resolves_to": route.slug,
            "reason": route.reason,
        },
    })

    return JSONResponse({
        "object": "list",
        "data": data,
        # Ollama-shaped mirror: llama.cpp emits both keys, and clients differ
        # in which one they read.
        "models": [{"name": e["id"], "model": e["id"]} for e in data],
        "x_aria_backend": {"slug": route.slug, "base_url": route.base_url},
    })


@router.get("/backend")
async def current_backend(
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Any:
    """Which server this proxy is currently forwarding to, and why.

    Not an OpenAI route — a diagnostic, so `scripts/health` and a human can see
    the resolution without inferring it from a completion.
    """
    route = await _pick_backend(manager, db)
    return {
        "backend": route.slug,
        "base_url": route.base_url,
        "reason": route.reason,
        "pinned": await read_pin(db),
        "admission": await _admission_snapshot(route),
        "running": [
            {"slug": s["slug"], "resident_gib": s.get("resident_gib_estimate")}
            for s in route.servers if s.get("state") == "running" and s.get("onbox")
        ],
    }


async def _autostart(
    manager: ModelServerManager,
    db: AsyncIOMotorDatabase,
    requested: str,
) -> bool:
    """Make an explicitly-named-but-stopped model resident. True if it started.

    This is what lets a consumer treat ARIA like a normal provider: pick a model
    and use it, rather than pick a model, discover it is not running, go start it
    somewhere else, come back. It fires ONLY when the caller named a specific
    registered server — never for `aria-resident`/auto, where "whatever is up" is
    the whole point and evicting a running model would be absurd.

    Conflicts are resolved by STOPPING them explicitly and then starting without
    `force`. Passing force=True would be wrong twice over: it skips the
    exclusivity check without freeing anything, and it also skips the live-GTT
    projection — the gate that stops us overcommitting the box.
    """
    from aria.infrastructure.model_servers import _BY_SLUG  # local: avoids a cycle

    # Acting, not routing: the exclusive-stop decision needs a live answer,
    # so this is the one proxy path that forces a full, uncached status().
    servers = await manager.status(db, force=True)
    target = None
    for server in servers:
        if _norm_slug(server.get("slug")) == _norm_slug(requested) or _norm_slug(
            (server.get("model_file") or "").rsplit("/", 1)[-1]
        ) == _norm_slug(requested):
            target = server
            break
    if target is None:
        return False

    slug = target["slug"]
    spec = _BY_SLUG.get(slug)
    if spec is None or not spec.onbox:
        return False

    by_slug = {s.get("slug"): s for s in servers}
    for other_slug in spec.exclusive_with:
        other = by_slug.get(other_slug)
        if not other or other.get("state") != "running":
            continue
        logger.warning(
            "llm-proxy: autostart %s — stopping exclusive server %s first", slug, other_slug
        )
        try:
            await manager.stop(other_slug, db=db)
        except Exception as exc:
            logger.error("llm-proxy: autostart %s aborted; %s would not stop: %s",
                         slug, other_slug, exc)
            return False

    logger.warning("llm-proxy: autostarting %s on demand (named by caller)", slug)
    try:
        # force=False on purpose — the GTT projection gate must still apply.
        await manager.start(slug, db=db)
    except Exception as exc:
        logger.error("llm-proxy: autostart of %s failed: %s", slug, exc)
        return False

    # `start` returns once the unit/container is up, but llama.cpp then spends
    # 90s-4min mapping weights and answers 503 "Loading model" throughout. A
    # caller that just picked a model should not have to poll — that is the
    # difference between "it works" and "it works if you retry".
    return await _await_ready(base_url_for(target) or "", slug)


async def _await_ready(base: str, slug: str) -> bool:
    """Poll a freshly started backend until it will actually answer."""
    if not base:
        return True  # nothing to poll; let the request try
    deadline = settings.llm_proxy_autostart_timeout
    waited = 0.0
    client = _client()
    while waited < deadline:
        try:
            resp = await client.get(f"{base}/models", timeout=5.0)
            if resp.status_code == 200:
                logger.warning("llm-proxy: %s ready after %.0fs", slug, waited)
                return True
        except httpx.HTTPError:
            pass
        await asyncio.sleep(2.0)
        waited += 2.0
    logger.error("llm-proxy: %s did not become ready within %.0fs", slug, deadline)
    return True  # started but slow — let the request through and surface the backend's own error


def _norm_slug(value: Optional[str]) -> str:
    return (value or "").strip().casefold().removesuffix(".gguf")


# A backend's ground-truth model id only changes when that process loads a
# different model, but fetching it is a tunnel round trip on every request.
# A short cache is invisible next to a model swap: the swap restarts the
# backend, which voids its prompt cache anyway.
_BACKEND_MODEL_ID_TTL_SECONDS = 30.0
_backend_model_id_cache: dict[str, tuple[float, Optional[str]]] = {}


async def _backend_model_id_cached(base: str) -> Optional[str]:
    if not base:
        return None
    hit = _backend_model_id_cache.get(base)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _BACKEND_MODEL_ID_TTL_SECONDS:
        return hit[1]
    model_id = await _backend_model_id(base)
    _backend_model_id_cache[base] = (now, model_id)
    return model_id


def _names_a_model(requested: Optional[str]) -> bool:
    """Did the caller name a concrete model, rather than the auto alias?

    A caller that named one already knows what it is talking to; only the alias
    leaves it unable to answer "what model are you?".
    """
    from aria.infrastructure.llm_route import AUTO_ALIASES, _norm

    if not requested:
        return False
    return _norm(requested) not in AUTO_ALIASES


def _identity_line(route: "_Route", model_id: Optional[str]) -> str:
    """The one sentence injected on the identified route."""
    name = model_id or route.slug or "an unnamed local model"
    return (
        f"You are running on {name}, served locally on this machine by ARIA. "
        f"If you are asked what model or LLM you are, say that. "
        f"'aria-resident' is ARIA's routing alias for whichever model is "
        f"currently resident — it is not the name of a model."
    )


def _inject_identity(body: dict, line: str) -> dict:
    """Prepend the identity line to the conversation's system context.

    Merged into an existing leading system message rather than added as a second
    one: some backends and templates only honour the first system turn.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        # /completions (raw prompt) has no message list to merge into. Leave it
        # alone rather than mangling the prompt string.
        return body

    out = list(messages)
    if out and isinstance(out[0], dict) and out[0].get("role") == "system":
        first = dict(out[0])
        existing = first.get("content")
        if isinstance(existing, str):
            first["content"] = f"{line}\n\n{existing}"
            out[0] = first
            body["messages"] = out
            return body
        # Structured/multipart content — don't try to splice it, prepend instead.
    out.insert(0, {"role": "system", "content": line})
    body["messages"] = out
    return body


async def _proxy(path: str, request: Request, manager: ModelServerManager,
                 db: AsyncIOMotorDatabase, identify: bool = False) -> Any:
    started = time.monotonic()
    payload = await request.body()
    stream = False
    requested: Optional[str] = None
    body: Optional[dict] = None
    model_id: Optional[str] = None
    try:
        body = json.loads(payload or b"{}")
        stream = bool(body.get("stream"))
        model = body.get("model")
        requested = model if isinstance(model, str) else None
    except (ValueError, AttributeError):
        body = None

    route = await _pick_backend(manager, db, requested=requested)

    # Named a registered server that isn't resident? Make it resident. Only for
    # an explicit name — `route.unavailable` is set exactly in that case, and is
    # never set for the auto alias.
    if route.unavailable and requested and settings.llm_proxy_autostart:
        if await _autostart(manager, db, requested):
            _drop_summary_cache()
            route = await _pick_backend(manager, db, requested=requested)

    if not route.base_url:
        exc = _unavailable(route)
        await _record_gateway_usage(
            db,
            request=request,
            route=route,
            requested_model=requested,
            backend_model_id=None,
            path=path,
            identify=identify,
            started=started,
            status_code=exc.status_code,
            error="backend unavailable",
        )
        raise exc
    slug, base = route.slug, route.base_url
    caller = _gateway_caller(request)[0]

    # ARIA model names are routing identifiers, not necessarily the id exposed
    # by the selected OpenAI-compatible backend.  vLLM rejects an unknown model
    # instead of ignoring it, so resolve the backend's ground-truth id and
    # rewrite the forwarded request after routing.
    if isinstance(body, dict):
        model_id = await _backend_model_id_cached(base)
        try:
            forwarded = dict(body)
            if model_id:
                forwarded["model"] = model_id

            # Inject only when the caller could NOT have known the model — i.e.
            # it used the auto alias.  A concrete ARIA model name is already an
            # explicit declaration and does not need a repeated system line.
            if identify and not _names_a_model(requested):
                forwarded = _inject_identity(
                    forwarded, _identity_line(route, model_id)
                )
            payload = json.dumps(forwarded).encode()
        except (TypeError, ValueError):
            logger.warning("llm-proxy: model rewrite failed; forwarding verbatim")

    url = f"{base}/{path}"
    headers = _forward_headers(request)

    if not stream:
        admission_stats: Optional[_AdmissionStats] = None
        try:
            async with _admit(route, caller) as admission_stats:
                resp = await _client().post(url, content=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("llm-proxy: %s failed against %s: %s", path, slug, exc)
            await _record_gateway_usage(
                db,
                request=request,
                route=route,
                requested_model=requested,
                backend_model_id=model_id,
                path=path,
                identify=identify,
                started=started,
                status_code=502,
                error=type(exc).__name__,
                admission=admission_stats,
            )
            raise HTTPException(
                status_code=502,
                detail={"error": f"backend {slug} unreachable", "backend": base},
            ) from exc

        response_payload: Optional[dict] = None
        if resp.headers.get("content-type", "").startswith("application/json"):
            try:
                decoded = resp.json()
            except ValueError:
                decoded = {"raw": resp.text}
            if isinstance(decoded, dict):
                response_payload = decoded
            content = decoded
        else:
            content = {"raw": resp.text}
        await _record_gateway_usage(
            db,
            request=request,
            route=route,
            requested_model=requested,
            backend_model_id=model_id,
            path=path,
            identify=identify,
            started=started,
            status_code=resp.status_code,
            response_payload=response_payload,
            admission=admission_stats,
        )
        return JSONResponse(
            content=content,
            status_code=resp.status_code,
            headers={
                "X-Aria-Backend": slug or "unknown",
                "X-Aria-Queue-Ms": str(admission_stats.queue_wait_ms),
            },
        )

    # Streaming: the shared client is held open for the life of the upstream
    # response; the connection returns to the pool when the stream ends.
    async def relay():
        usage = _StreamUsage()
        status_code = 200
        error: Optional[str] = None
        admission_stats: Optional[_AdmissionStats] = None
        try:
            async with _admit(route, caller) as admission_stats:
                client = _client()
                async with client.stream("POST", url, content=payload, headers=headers) as resp:
                    status_code = resp.status_code
                    async for chunk in resp.aiter_raw():
                        usage.feed(chunk)
                        yield chunk
        except httpx.HTTPError as exc:
            logger.warning("llm-proxy: stream to %s failed: %s", slug, exc)
            status_code = 502
            error = type(exc).__name__
            yield b'data: {"error":"backend stream failed"}\n\n'
        finally:
            usage.finish()
            # A client disconnect cancels the response generator. Shield the
            # small Mongo write so interrupted streams still leave an audit row.
            record_task = asyncio.create_task(_record_gateway_usage(
                db,
                request=request,
                route=route,
                requested_model=requested,
                backend_model_id=model_id,
                path=path,
                identify=identify,
                started=started,
                status_code=status_code,
                response_payload=usage.payload,
                streamed=True,
                error=error,
                admission=admission_stats,
            ))
            try:
                await asyncio.shield(record_task)
            except asyncio.CancelledError:
                # The shielded task remains live on the application event loop.
                raise

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Aria-Backend": slug or "unknown"},
    )


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Any:
    return await _proxy("chat/completions", request, manager, db)


@router.post("/completions")
async def completions(
    request: Request,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Any:
    return await _proxy("completions", request, manager, db)


# --- the identified route -------------------------------------------------
# Byte-identical routing to /llm/v1; the only difference is the injected system
# line. /models and /backend are re-exported so a client pointed here has a
# complete OpenAI surface and health probes still work against this base_url.

@identified_router.post("/chat/completions")
async def chat_completions_identified(
    request: Request,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Any:
    return await _proxy("chat/completions", request, manager, db, identify=True)


@identified_router.post("/completions")
async def completions_identified(
    request: Request,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Any:
    # Raw-prompt completions carry no message list, so nothing is injected —
    # forwarded exactly as /llm/v1 would. Present so this base_url is complete.
    return await _proxy("completions", request, manager, db, identify=True)


@identified_router.get("/models")
async def list_models_identified(
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Any:
    return await list_models(manager, db)


@identified_router.get("/backend")
async def current_backend_identified(
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Any:
    return await current_backend(manager, db)
