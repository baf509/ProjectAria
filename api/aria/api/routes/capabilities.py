"""
ARIA - Retrieval Capability Routes

Purpose: Turn mongot and the embeddings model off/on at runtime, and see what
         that is currently costing.

Related: memory/capabilities.py (the switches), memory/backfill.py (the drain),
         infrastructure/services.py (the containers themselves).

The switch and the container are deliberately separate concerns — ARIA can keep
serving with mongot up but unused, or (the usual case) with it stopped to free
the box. `with_service=true` on the PUT does both in the right order, which is
the ordering that matters:

    disabling → flip the switch FIRST, then stop the container. The other way
      round leaves a window where live requests still dial a dead service.
    enabling  → start the container FIRST, then flip the switch. Otherwise the
      first request after the flip races the container's startup.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_db
from aria.memory.capabilities import retrieval_capabilities

logger = logging.getLogger(__name__)

router = APIRouter()

# Which registry row backs each capability, for the optional container control.
_SERVICE_FOR = {"embeddings": "shared-embeddings", "search": "shared-mongot"}


class RetrievalCapabilityUpdate(BaseModel):
    embeddings: Optional[bool] = Field(
        default=None,
        description="Call the embeddings service? False: memories are still stored, "
        "flagged embedding_pending, and re-embedded when this flips back to True.",
    )
    search: Optional[bool] = Field(
        default=None,
        description="Emit $vectorSearch/$search (mongot)? False: recall degrades to "
        "the mongod-native fallback scan.",
    )
    reason: str = Field(default="", description="Why — recorded with the flip.")
    changed_by: str = Field(default="api")
    with_service: bool = Field(
        default=False,
        description="Also stop/start the backing container (shared-embeddings / "
        "shared-mongot) via the non-LLM service registry.",
    )


def _worker(request: Request):
    return getattr(request.app.state, "embedding_backfill", None)


async def _service_states() -> dict:
    """Live container state for the two backing services, best-effort."""
    try:
        from aria.infrastructure.services import get_service_manager

        rows = {s["slug"]: s for s in await get_service_manager().status()}
    except Exception as exc:  # noqa: BLE001 — advisory only, never fatal
        logger.debug("capabilities: service registry unavailable: %s", exc)
        return {}
    return {
        cap: {"slug": slug, "state": rows.get(slug, {}).get("state", "unknown")}
        for cap, slug in _SERVICE_FOR.items()
    }


@router.get("/capabilities/retrieval")
async def get_retrieval_capabilities(request: Request):
    """Current switches, what recall is doing, and how big the backlog is."""
    status = retrieval_capabilities.status()
    status["services"] = await _service_states()

    worker = _worker(request)
    if worker is not None:
        status["backfill"] = {
            "running": True,
            "interval_seconds": worker.interval,
            "batch_size": worker.batch_size,
            "last_run_at": worker.last_run_at,
            "last_result": worker.last_result,
            "pending": await worker.pending_counts(),
        }
    else:
        status["backfill"] = {"running": False, "detail": "backfill worker disabled"}
    return status


@router.put("/capabilities/retrieval")
async def set_retrieval_capabilities(
    body: RetrievalCapabilityUpdate,
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Flip either switch (or both). Omitted fields are left untouched.

    Re-enabling embeddings kicks the backfill worker immediately, so everything
    written during the outage is re-embedded without a second call.
    """
    if body.embeddings is None and body.search is None:
        raise HTTPException(
            status_code=400, detail="Set at least one of 'embeddings' or 'search'"
        )

    retrieval_capabilities.set_db(db)
    actions: list[str] = []

    # Enabling: container first, then the switch (see module docstring).
    if body.with_service:
        for cap, wanted in (("embeddings", body.embeddings), ("search", body.search)):
            if wanted is True:
                actions.append(await _drive_service(cap, start=True))

    if body.search is not None:
        await retrieval_capabilities.set_search(
            body.search, reason=body.reason, changed_by=body.changed_by
        )
    if body.embeddings is not None:
        await retrieval_capabilities.set_embeddings(
            body.embeddings, reason=body.reason, changed_by=body.changed_by
        )

    # Disabling: switch first, then stop the container.
    if body.with_service:
        for cap, wanted in (("embeddings", body.embeddings), ("search", body.search)):
            if wanted is False:
                actions.append(await _drive_service(cap, start=False))

    result = await get_retrieval_capabilities(request)
    if actions:
        result["service_actions"] = actions
    return result


async def _drive_service(capability: str, *, start: bool) -> str:
    """Start/stop the container behind a capability. Never fatal to the flip.

    A failed container transition must not roll back the switch: the switch is
    what keeps ARIA serving, and reverting it because docker was slow would
    leave the operator with the exact broken state they were escaping.
    """
    slug = _SERVICE_FOR[capability]
    verb = "start" if start else "stop"
    try:
        from aria.infrastructure.services import get_service_manager

        mgr = get_service_manager()
        await (mgr.start(slug) if start else mgr.stop(slug))
        return f"{'started' if start else 'stopped'} {slug}"
    except Exception as exc:  # noqa: BLE001
        # Don't take the exception's word for it. The registry's docker call
        # times out at 10s, but `docker stop` itself waits 10s for SIGTERM
        # before SIGKILL — so a container that ignores SIGTERM reliably reports
        # a timeout *and then stops anyway* (observed on shared-embeddings,
        # exit 137). Reporting that as "failed to stop" tells the operator to
        # go fix something that is already done. Re-read the real state.
        try:
            from aria.infrastructure.services import get_service_manager

            state = (await get_service_manager().get(slug)).get("state")
            reached = state in ("running", "active") if start else state not in ("running", "active")
            if reached:
                logger.info(
                    "capabilities: %s %s reported an error (%s) but reached state %r",
                    verb, slug, exc, state,
                )
                return f"{slug} is {state} (the {verb} call reported: {exc})"
        except Exception as probe_exc:  # noqa: BLE001
            logger.debug("capabilities: could not re-check %s state: %s", slug, probe_exc)

        logger.warning("capabilities: could not %s %s: %s", verb, slug, exc)
        return f"failed to {verb} {slug}: {exc}"


@router.post("/capabilities/retrieval/backfill")
async def run_embedding_backfill(request: Request, batch_size: Optional[int] = None):
    """Run one backfill pass now and return what it did.

    Synchronous on purpose — the caller asked for a catch-up and should see it
    finish (or its error), same reasoning as the shells extraction backfill.
    Call it repeatedly to drain a large backlog; each pass is bounded.
    """
    worker = _worker(request)
    if worker is None:
        raise HTTPException(
            status_code=409,
            detail="Backfill worker is not running (embedding_backfill_enabled is false)",
        )
    if not retrieval_capabilities.embeddings_enabled:
        raise HTTPException(
            status_code=409,
            detail="Embeddings capability is disabled — enable it first "
            "(PUT /api/v1/capabilities/retrieval {\"embeddings\": true})",
        )
    return await worker.run_once(batch_size=batch_size)
