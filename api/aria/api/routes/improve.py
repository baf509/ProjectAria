"""
ARIA - Improver Routes

Purpose: the human surface over `policy_versions` — what has ARIA proposed to
change about itself, what evidence does each proposal carry, and the three
decisions Ben can make about one (APPLY / REJECT / ROLLBACK).

**Why the split between open reads and admin-keyed writes.** Reading proposals
is how the cockpit, the TUI and the digest show what the improver is thinking;
those are cheap and side-effect-free, so they sit behind the global API key like
every other read. Promotion and rollback *write the live policy surface* — the
prompt files and agent system prompts that every subsequent agent turn inherits.
The global `API_KEY` is readable by anything running as `ben`, coding agents
included (steward plan §7.3), so it cannot be what stands between an agent and
its own prompt. Those two go behind `require_admin`, which is never placed in a
session environment.

Rejecting is *not* admin-keyed on purpose: it is the safe direction (it can only
stop a change from happening), and making it harder than promoting would push
the operator toward the dangerous default.

**A proposal can never be promoted without a passing gate**, admin key or not.
The whole scope rule of §8 is "only things with an automatic evaluator may
self-modify"; an override flag here would be a door around it, and the doors
around evaluators are what every published self-improvement failure walked
through.
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_db, require_admin
from aria.config import settings
from aria.steward.improve import (
    POLICY_VERSIONS_COLLECTION,
    STATUSES,
    ImproverError,
    NeedsHuman,
    PolicyVersionStore,
    collect_baseline,
    fixture_path,
    mutable_paths,
    mutable_thresholds,
    serialize_version,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/improve", tags=["improve"])


class RejectRequest(BaseModel):
    reason: str = Field(default="rejected by operator", max_length=2000)
    by: str = Field(default="ben")


class RollbackRequest(BaseModel):
    reason: str = Field(default="manual rollback", max_length=2000)
    by: str = Field(default="ben")


class PromoteRequest(BaseModel):
    by: str = Field(default="ben")


def _store(db: AsyncIOMotorDatabase) -> PolicyVersionStore:
    return PolicyVersionStore(db)


def _improver(request: Request):
    """The live worker, when main.py's lifespan wired one up.

    Absent it, the read routes still work off the collection — the version rows
    are the record, not the worker's memory — and only the two routes that need
    the worker itself say so.
    """
    return getattr(request.app.state, "improver", None)


@router.get("/status")
async def improve_status(
    request: Request,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Is the improver on, what may it touch, and where does it stand?"""
    worker = _improver(request)
    if worker is not None:
        return await worker.status()

    counts: dict = {}
    try:
        for status in STATUSES:
            counts[status] = await db[POLICY_VERSIONS_COLLECTION].count_documents(
                {"status": status}
            )
    except Exception as exc:  # noqa: BLE001 - status must answer without Mongo
        counts = {"error": str(exc)}
    return {
        "enabled": settings.improver_enabled,
        "worker": False,
        "interval_hours": settings.improver_interval_hours,
        "max_proposals_per_run": settings.improver_max_proposals_per_run,
        "mutable_paths": mutable_paths(),
        "mutable_thresholds": mutable_thresholds(),
        "eval_fixture": fixture_path(),
        "counts": counts,
    }


@router.get("/baseline")
async def improve_baseline(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    days: Optional[int] = None,
):
    """The outcome data the improver is allowed to reason about.

    Exposed because "why did it propose nothing this week" is the most common
    question this subsystem will get, and `labelled_outcomes` is nearly always
    the answer.
    """
    baseline = await collect_baseline(db, days=days)
    return baseline.to_dict()


@router.get("/proposals")
async def list_proposals(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    status: Optional[str] = None,
    target: Optional[str] = None,
    limit: int = 50,
):
    """Policy versions, newest first. Bodies are summarised; use the detail
    route for the full before/after and the gate evidence."""
    if status and status not in STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {', '.join(STATUSES)}",
        )
    docs = await _store(db).list(status=status, target=target, limit=max(1, min(200, limit)))
    return {"proposals": [serialize_version(d) for d in docs], "count": len(docs)}


@router.get("/proposals/{version_id}")
async def get_proposal(
    version_id: str,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """One proposal with its full text and gate evidence — the diff, the fixture
    scores before and after, and the different-family judge's verdict."""
    doc = await _store(db).get(version_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"No policy version {version_id}")
    return serialize_version(doc, with_evidence=True)


@router.post("/proposals/{version_id}/promote")
async def promote_proposal(
    version_id: str,
    body: PromoteRequest,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    _admin: Annotated[bool, Depends(require_admin)],
):
    """Ben's `APPLY <id>`: write the candidate to the live target.

    Refuses without a passing gate, and refuses if the target has drifted since
    the proposal was made (somebody edited it meanwhile — overwriting a human
    edit is the ownership violation this codebase avoids everywhere else).
    """
    try:
        doc = await _store(db).promote(version_id, actor=body.by or "ben")
    except NeedsHuman as exc:
        # The target left the mutable surface between proposal and APPLY. That
        # is a refusal, not a 500: the policy tightened and the write is denied.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ImproverError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "proposal": serialize_version(doc)}


@router.post("/proposals/{version_id}/reject")
async def reject_proposal(
    version_id: str,
    body: RejectRequest,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Ben's `REJECT <id>`. Safe direction: nothing is written to the target."""
    try:
        doc = await _store(db).reject(version_id, reason=body.reason, actor=body.by or "ben")
    except ImproverError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "proposal": serialize_version(doc)}


@router.post("/proposals/{version_id}/rollback")
async def rollback_proposal(
    version_id: str,
    body: RollbackRequest,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    _admin: Annotated[bool, Depends(require_admin)],
):
    """Restore `before` verbatim — the undo the version row exists to be.

    Admin-keyed for the same reason promote is: it writes the live policy
    surface. The content being replaced is kept on the row, so a rollback is
    itself reversible.
    """
    try:
        doc = await _store(db).rollback(version_id, actor=body.by or "ben",
                                        reason=body.reason)
    except ImproverError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "proposal": serialize_version(doc)}


@router.post("/run")
async def run_improver(
    request: Request,
    _admin: Annotated[bool, Depends(require_admin)],
):
    """Run one improvement tick now.

    Admin-keyed although the tick itself only *proposes*: it spends model time,
    can start an isolated worktree and a full pytest run, and — once a target
    class has earned auto-apply — can write a prompt. That is not a read.
    """
    worker = _improver(request)
    if worker is None:
        raise HTTPException(
            status_code=503,
            detail="No improver worker on app.state (improver_enabled is off, or "
                   "the lifespan has not wired one — see the INTEGRATION SPEC).",
        )
    return await worker.run_once()
