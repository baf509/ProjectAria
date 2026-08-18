"""
ARIA - Infrastructure Routes

Purpose: Model-server control plane (start/stop/bind the local LLM servers,
pull + provision new ones from Hugging Face).
See aria.infrastructure.model_servers / model_pull for the registry, the
safety gates, and the provisioning pipeline.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import (
    get_db,
    get_model_pull_service,
    get_model_server_manager,
    get_service_manager,
    require_admin,
)
from aria.infrastructure.llm_route import (
    backend_model_id as _backend_model_id,
    base_url_for,
    is_servable,
    match_requested,
    read_pin,
    select,
    write_pin,
)
from aria.infrastructure.gpu_devices import (
    device_snapshot,
    pool_snapshot,
    system_memory_snapshot,
)
from aria.infrastructure.model_pull import RUNTIME_TEMPLATES, ModelPullService
from aria.infrastructure.model_servers import (
    _BY_SLUG,
    ModelServerBindingConflict,
    ModelServerError,
    ModelServerManager,
    ModelServerNotFound,
    ModelServerSafetyError,
    check_pi_slot_budget,
    probe_runtime,
)
from aria.infrastructure.services import (
    ServiceError,
    ServiceManager,
    ServiceNotFound,
    ServiceNotManageable,
    review_needed,
)

router = APIRouter(prefix="/infrastructure", tags=["infrastructure"])


class StartStopRequest(BaseModel):
    force: bool = False
    overrides: Optional[dict[str, str]] = Field(
        default=None,
        description=(
            "Launch parameters to apply, keyed by the `parameters[].name` values "
            "the server reports — device placement, context, KV type, drafter, "
            "slots. Omit to start with the deployment's own defaults, which also "
            "CLEARS any override ARIA applied on a previous start."
        ),
    )


class BindRequest(BaseModel):
    agent: str
    force: bool = False


class UnbindRequest(BaseModel):
    agent: str


class PullRequest(BaseModel):
    repo_id: str = Field(description="Hugging Face repo, e.g. unsloth/Qwen3.6-27B-MTP-GGUF")
    filename: str = Field(description="Exact .gguf filename inside the repo")
    name: str = Field(description="Registry slug / directory name for the new server")
    runtime: str = Field(description=f"One of {sorted(RUNTIME_TEMPLATES)}")
    port: Optional[int] = Field(default=None, description="Host port; auto-allocated from 8105+ if omitted")
    ctx: int = Field(default=32768, ge=512, le=1048576, description="--ctx-size for the generated service")


class LlmRouteRequest(BaseModel):
    slug: Optional[str] = Field(
        default=None,
        description="Server to serve as the local model; null/'auto' follows whichever is resident",
    )


@router.get("/llm-route")
async def get_llm_route(
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Who currently answers as 'the local model', and whether that is pinned.

    This is the read side of the knob that decides what `LLAMACPP_URL` — and
    therefore Hermes, which follows the same passthrough — actually talks to
    when more than one server is resident.

    Also answers the identity question "what model am I actually running on?".
    Consumers configured against the passthrough see only the synthetic id
    `aria-resident`, so asked what they are they will describe their *config*
    ("aria-resident on a custom provider") rather than the model. `model_id`
    here is the loaded model as the backend itself reports it — ground truth,
    read live from the server rather than from this registry — and `summary`
    is a ready-to-say sentence built from it.
    """
    servers = await manager.status(db)
    pin = await read_pin(db)
    chosen, reason, _ = select(servers, pin=pin)

    model_id = await _backend_model_id(base_url_for(chosen) or "") if chosen else None
    slug = chosen.get("slug") if chosen else None

    if slug:
        # Prefer the backend's own answer; fall back to the registry's filename
        # if the server is up but /v1/models did not answer.
        name = model_id or (chosen.get("model_file") or "").rsplit("/", 1)[-1] or slug
        summary = (
            f"Running on {name} (ARIA registry slug '{slug}', "
            f"{chosen.get('backend_device') or 'local'}), served locally through "
            f"ARIA's /llm/v1 passthrough. 'aria-resident' is the routing alias, "
            f"not the model."
        )
    else:
        summary = "No local model server is currently resident."

    return {
        "pinned": pin,
        "serving": slug,
        # Ground truth from the backend, for "which model am I?".
        "model_id": model_id,
        "model_file": chosen.get("model_file") if chosen else None,
        "backend_device": chosen.get("backend_device") if chosen else None,
        "summary": summary,
        "reason": reason,
        "loaded": [
            {
                "slug": s["slug"],
                "resident_gib": s.get("resident_gib_estimate"),
                "backend_device": s.get("backend_device"),
            }
            for s in servers
            if is_servable(s)
        ],
    }


# The route pin decides which model every /llm/v1 consumer reaches; an
# agent that could move it could point ARIA's own workers at a model of
# its choosing.
@router.put("/llm-route", dependencies=[Depends(require_admin)])
async def set_llm_route(
    body: LlmRouteRequest,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Pin the local-model route to one server, or clear it back to auto.

    Pinning a *stopped* server is refused: silently accepting it would leave
    every consumer on a different model than the one the UI claims is selected.
    """
    slug = (body.slug or "").strip() or None
    if slug and slug.lower() in {"auto", "aria-resident"}:
        slug = None

    if slug is not None:
        servers = await manager.status(db)
        chosen, stopped = match_requested(servers, slug)
        if chosen is None:
            known = sorted(s["slug"] for s in servers)
            raise HTTPException(
                status_code=409 if stopped else 404,
                detail=(
                    f"{slug} is not running — start it before pinning."
                    if stopped
                    else f"unknown model server '{slug}' (known: {', '.join(known)})"
                ),
            )
        slug = chosen["slug"]

    await write_pin(db, slug)
    return await get_llm_route(manager=manager, db=db)


@router.get("/running")
async def running_overview(
    manager: ModelServerManager = Depends(get_model_server_manager),
    services: ServiceManager = Depends(get_service_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """One answer to "what is running on this box?" across BOTH registries.

    A union read, deliberately not a merged control plane: model servers and
    non-LLM services keep separate registries (see
    aria/infrastructure/services.py for why merging them silences the alerts
    this endpoint exists to surface), but an operator asking what is up should
    not have to check two places.

    `unhealthy` counts only services expected to be up and aren't. A stopped
    model server is normal — they are mutually RAM-exclusive — and a stopped
    on_demand service is normal too, so neither counts against it.
    """
    try:
        svc = await services.status(db)
    except Exception as exc:  # noqa: BLE001 — never let one side blank the view
        svc, svc_error = [], str(exc)
    else:
        svc_error = None

    try:
        model_servers = await manager.status(db)
    except ModelServerError as exc:
        model_servers, ms_error = [], str(exc)
    else:
        ms_error = None

    unhealthy = [s for s in svc if not s["healthy"]]
    return {
        "services": svc,
        "model_servers": [
            {
                "slug": s["slug"],
                "state": s["state"],
                "port": s["port"],
                "backend_device": s.get("backend_device"),
                "resident_gib": s.get("resident_gib_estimate"),
            }
            for s in model_servers
        ],
        "running": {
            "services": [s["slug"] for s in svc if s["state"] == "running"],
            "model_servers": [
                s["slug"] for s in model_servers if s["state"] == "running"
            ],
        },
        "unhealthy": [
            {"slug": s["slug"], "state": s["state"], "expected": s["expected_state"]}
            for s in unhealthy
        ],
        "needs_review": [s.slug for s in review_needed()],
        "errors": {k: v for k, v in (("services", svc_error), ("model_servers", ms_error)) if v},
    }


@router.get("/services")
async def list_services(
    services: ServiceManager = Depends(get_service_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Non-LLM services: state, expected_state, and the health verdict."""
    entries = await services.status(db)
    return {
        "services": entries,
        "healthy": sum(1 for s in entries if s["healthy"]),
        "total": len(entries),
    }


@router.get("/services/{slug}")
async def get_service(
    slug: str,
    services: ServiceManager = Depends(get_service_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    try:
        return await services.get(slug, db)
    except ServiceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/services/{slug}/start")
async def start_service(
    slug: str,
    services: ServiceManager = Depends(get_service_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    try:
        return await services.start(slug, db)
    except ServiceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ServiceNotManageable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ServiceError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/services/{slug}/stop")
async def stop_service(
    slug: str,
    services: ServiceManager = Depends(get_service_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    try:
        return await services.stop(slug, db)
    except ServiceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ServiceNotManageable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ServiceError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# What a fleet LIST needs, as opposed to a server detail. The full row is 78 KB
# across 27 servers and ~84% of that is static registry prose — `parameters`,
# `description`, `exclusive_with`, `not_startable_reason` — which never changes
# between polls but was being re-sent on every one, to a phone, over the
# tailnet. The same fields for a list view come to 12.6 KB.
_LIST_VIEW_FIELDS = frozenset({
    "slug", "state", "port", "onbox", "startable", "weights_present",
    "backend_device", "memory_pool", "also_uses",
    "resident_gib_estimate", "resident_gib_measured",
    "pool_used_gib", "pool_total_gib", "pool_spilling",
    "gtt_used_gib", "gtt_total_gib", "gtt_resident",
    "served_ctx", "slots", "ctx_per_slot",
    "bound_agents", "remotely_operable", "can_sleep", "serving",
})


@router.get("/model-servers")
async def list_model_servers(
    view: str = "full",
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """The fleet.

    `view=list` returns only the fields a list can render. It exists because
    this is the most-polled heavy endpoint in the app and most of its weight is
    registry text that a list never displays; the detail view fetches the full
    row for one server from `/model-servers/{slug}`. Default stays `full` so no
    existing consumer changes behaviour.
    """
    try:
        servers = await manager.status(db)
    except ModelServerError as exc:
        # e.g. docker daemon unreachable — surfaced, not masked as not_created
        raise HTTPException(status_code=503, detail=str(exc))
    if view == "list":
        servers = [{k: v for k, v in s.items() if k in _LIST_VIEW_FIELDS} for s in servers]
    return {"servers": servers}


@router.get("/model-servers/utilization")
async def model_server_utilization(
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Live slot occupancy + throughput for every RUNNING on-box server.

    Answers "how loaded is the local fleet right now", which the static
    `/model-servers` view cannot: that reports how many slots *should* exist,
    this reports how many are busy. Declared here (before `/{slug}`) so the
    literal path is not captured as a slug.

    Backend-aware since 2026-08-17: llama.cpp (`/slots` + `/metrics`), vLLM
    (Prometheus `/metrics`, no `/slots`) and DwarfStar (`/v1/models` only, no
    telemetry at all) each report what they actually have. `runtime_family`
    says which answered and `telemetry_hint` says why a field is missing —
    ⚠️ a null here has always meant UNKNOWN, never "not busy".

    `saturated` is the field to watch. It means requests are QUEUING because
    every slot was taken — and a queued request lands in whichever slot frees
    first, not necessarily the one holding its prefix, so sustained saturation
    is how warm caches quietly degrade into a cold prefill per turn.
    """
    try:
        servers = await manager.status(db)
    except ModelServerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    running = [s for s in servers if s.get("state") == "running" and s.get("onbox")]
    specs = [_BY_SLUG.get(s["slug"]) for s in running]
    probes = await asyncio.gather(
        *(probe_runtime(sp) for sp in specs if sp is not None),
        return_exceptions=True,
    )

    out = []
    _pairs = [(s, sp) for s, sp in zip(running, specs) if sp is not None]
    for (server, _sp), stats in zip(_pairs, probes):
        if isinstance(stats, BaseException) or stats is None:
            # ⚠️ A probe that RAISED and a server that is DOWN both land here and
            # are indistinguishable to a reader. `probe_error` separates them —
            # an AttributeError in the probe read as "radiance is unreachable"
            # on 2026-08-17 and sent the diagnosis in the wrong direction.
            out.append({
                "slug": server["slug"],
                "reachable": False,
                "probe_error": (f"{type(stats).__name__}: {stats}"[:200]
                                if isinstance(stats, BaseException) else None),
                "bench_decode_tok_s": _sp.bench_decode_tok_s if _sp else None,
                "bench_prefill_tok_s": _sp.bench_prefill_tok_s if _sp else None,
                "benchmarked_at": _sp.bench_at if _sp else None,
            })
            continue
        out.append({
            "slug": server["slug"],
            "reachable": True,
            "busy_slots": stats.busy_slots,
            "total_slots": stats.total_slots,
            "free_slots": stats.free_slots,
            "slot_utilisation": stats.slot_utilisation,
            "ctx_per_slot": stats.ctx_per_slot,
            # Declared (from the launch file) vs observed (from the server).
            # A mismatch means the unit was edited without a restart.
            "declared_slots": server.get("slots"),
            "declared_ctx_per_slot": server.get("ctx_per_slot"),
            "saturated": stats.saturated,
            "requests_processing": stats.requests_processing,
            "requests_deferred": stats.requests_deferred,
            "prompt_tokens_per_second": stats.prompt_tokens_per_second,
            "predicted_tokens_per_second": stats.predicted_tokens_per_second,
            "avg_busy_slots_per_decode": stats.avg_busy_slots_per_decode,
            "prompt_tokens_total": stats.prompt_tokens_total,
            "tokens_predicted_total": stats.tokens_predicted_total,
            "metrics_available": stats.metrics_available,
            "metrics_hint": stats.metrics_hint,
            # --- cross-backend (2026-08-17) ---------------------------------
            # Which telemetry surface answered. Before this, every field below
            # came back null for vLLM and DwarfStar and there was no way to tell
            # "unknown" from "idle".
            "runtime_family": stats.runtime_family,
            "telemetry_hint": stats.telemetry_hint,
            "served_ctx": stats.served_ctx,
            # vLLM-only: it preallocates its KV pool, so this is true occupancy.
            "kv_cache_usage_pct": stats.kv_cache_usage_pct,
            # vLLM-only: whether prompt caching is actually paying off.
            "prefix_cache_hit_rate": stats.prefix_cache_hit_rate,
            "prompt_tokens_cached_total": stats.prompt_tokens_cached_total,
            # --- prompt-cache capacity: where it lives, how big it can get ---
            "prompt_cache_kind": stats.prompt_cache_kind,
            "prompt_cache_capacity": stats.prompt_cache_capacity,
            "prompt_cache_used": stats.prompt_cache_used,
            # ⚠️ vLLM: prefix caching is BLOCK-granular. Prompts sharing fewer
            # than this many leading tokens get NO reuse at all.
            "cache_block_size": stats.cache_block_size,
            "mean_ttft_seconds": stats.mean_ttft_seconds,
            # ── Last BENCHMARKED throughput, with the date it was taken ──────
            # Not live: no backend here reports a stable tok/s at rest. These are
            # recorded runs, and `benchmarked_at` is what tells you whether to
            # trust them — re-measure after any quant/runtime/context change.
            "bench_decode_tok_s": _sp.bench_decode_tok_s if _sp else None,
            "bench_prefill_tok_s": _sp.bench_prefill_tok_s if _sp else None,
            "benchmarked_at": _sp.bench_at if _sp else None,
            "bench_note": _sp.bench_note if _sp else None,
            "resident_gib_measured": server.get("resident_gib_measured"),
        })
    return {"servers": out, "slot_budget_warning": check_pi_slot_budget()}


@router.get("/model-servers/devices")
async def list_devices():
    """The physical GPUs on this box and the memory pools they own.

    Two GPUs with SEPARATE memory means "how full is the box" has two answers,
    and conflating them is a real failure mode rather than a rounding error:
    DRM enumeration puts the discrete R9700 at card0 and the Strix Halo iGPU
    at card1, so the historical hardcoded card0 read reports ~0 GiB used while
    the Halo is holding ~100. Declared before `/{slug}` so the literal path is
    not captured as a slug.

    `spilling` on a discrete card means it has started serving out of system
    RAM — at which point the pools are no longer independent and a co-resident
    Halo model is at risk.
    """
    # `system` is the composite that `pools` cannot express: halo-gtt and
    # host-ram are the same DIMMs, so a client drawing one bar per pool
    # double-counts ~102 GiB on this box.
    return {
        "devices": device_snapshot(),
        "pools": pool_snapshot(),
        "system": system_memory_snapshot(),
    }


@router.get("/model-servers/runtimes")
async def list_runtimes():
    """The runtime templates a pulled model can be provisioned onto."""
    return {
        "runtimes": [
            {"slug": slug, "description": t["description"], "image": t["image"]}
            for slug, t in RUNTIME_TEMPLATES.items()
        ]
    }


@router.get("/model-servers/pulls")
async def list_model_pulls(
    pull_service: ModelPullService = Depends(get_model_pull_service),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    return {"pulls": await pull_service.list_jobs(db)}


@router.post("/model-servers/pull")
async def pull_model(
    body: PullRequest,
    pull_service: ModelPullService = Depends(get_model_pull_service),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Download a GGUF from Hugging Face into infrastructure/models/llm/<name>/,
    generate a compose service on the chosen runtime, and register it as a
    startable model server."""
    try:
        return await pull_service.start_pull(
            db, body.repo_id, body.filename, body.name, body.runtime,
            port=body.port, ctx=body.ctx,
        )
    except ModelServerError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/model-servers/{slug}")
async def get_model_server(
    slug: str,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    # One-spec probe, not the full fleet: a single-entity read must not pay
    # the full status() cost (~70-80 subprocesses) to answer about one row.
    try:
        return await manager.one(slug, db)
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ModelServerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/model-servers/{slug}/start")
async def start_model_server(
    slug: str,
    body: StartStopRequest = StartStopRequest(),
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    try:
        return await manager.start(
            slug, force=body.force, db=db, overrides=body.overrides
        )
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ModelServerSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ModelServerError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/model-servers/{slug}/stop")
async def stop_model_server(
    slug: str,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    try:
        return await manager.stop(slug, db=db)
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ModelServerSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ModelServerError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/model-servers/{slug}/sleep")
async def sleep_model_server(
    slug: str,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Suspend an off-box machine (Ridge). Wake is automatic via its proxy."""
    try:
        return await manager.sleep(slug, db=db)
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ModelServerSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ModelServerError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/model-servers/{slug}/bind")
async def bind_model_server(
    slug: str,
    body: BindRequest,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    try:
        return await manager.bind(db, slug, body.agent, force=body.force)
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ModelServerBindingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/model-servers/unbind")
async def unbind_model_server(
    body: UnbindRequest,
    manager: ModelServerManager = Depends(get_model_server_manager),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Clear whatever model_server is currently bound to this agent, if any."""
    try:
        return await manager.unbind(db, body.agent)
    except ModelServerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
