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
import json
import logging
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.api.deps import get_db, get_model_server_manager
from aria.config import settings
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

# Hop-by-hop headers must not be forwarded (RFC 7230 6.1), plus the ones that
# would misdescribe the proxied body.
_STRIP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "accept-encoding",
}


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
    """Resolve request-model > operator pin > largest resident. See module docstring."""
    servers = await manager.status(db)
    pin = await read_pin(db)
    chosen, reason, unavailable = select(servers, requested=requested, pin=pin)
    if chosen is None:
        return _Route(None, None, reason, servers, unavailable)
    return _Route(chosen.get("slug"), base_url_for(chosen), reason, servers)


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
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{base}/models")
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

    servers = await manager.status(db)
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
    async with httpx.AsyncClient(timeout=5.0) as client:
        while waited < deadline:
            try:
                resp = await client.get(f"{base}/models")
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
    payload = await request.body()
    stream = False
    requested: Optional[str] = None
    body: Optional[dict] = None
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
            route = await _pick_backend(manager, db, requested=requested)

    if not route.base_url:
        raise _unavailable(route)
    slug, base = route.slug, route.base_url

    # Inject only when the caller could NOT have known the model — i.e. it used
    # the auto alias. When it named a concrete model it already knows what it is,
    # and a line telling it so is redundant tokens on every single turn.
    if identify and isinstance(body, dict) and not _names_a_model(requested):
        # Ground truth from the backend, falling back to the registry slug.
        model_id = await _backend_model_id(base)
        try:
            payload = json.dumps(_inject_identity(body, _identity_line(route, model_id))).encode()
        except (TypeError, ValueError):
            logger.warning("llm-proxy: identity injection failed; forwarding verbatim")

    # The body is forwarded verbatim, `model` included: llama.cpp ignores the
    # field and serves whatever it has loaded, so rewriting it would buy
    # nothing — and the response already carries the backend's own model id.
    url = f"{base}/{path}"
    headers = _forward_headers(request)

    if not stream:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, content=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("llm-proxy: %s failed against %s: %s", path, slug, exc)
            raise HTTPException(
                status_code=502,
                detail={"error": f"backend {slug} unreachable", "backend": base},
            ) from exc
        return JSONResponse(
            content=resp.json() if resp.headers.get("content-type", "").startswith("application/json")
            else {"raw": resp.text},
            status_code=resp.status_code,
        )

    # Streaming: hold the client open for the life of the upstream response.
    async def relay():
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                async with client.stream("POST", url, content=payload, headers=headers) as resp:
                    async for chunk in resp.aiter_raw():
                        yield chunk
        except httpx.HTTPError as exc:
            logger.warning("llm-proxy: stream to %s failed: %s", slug, exc)
            yield b'data: {"error":"backend stream failed"}\n\n'

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
