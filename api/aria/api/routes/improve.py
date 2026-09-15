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


# Weekly platform review retains the legacy policy-version routes above.
class WeeklyTrigger(BaseModel):
    policy_id: str = Field(default="platform", max_length=100)
    report_only: bool = False


class FindingDisposition(BaseModel):
    status: str = Field(pattern="^(dismissed|deferred|suggested)$")
    reason: str = Field(min_length=1, max_length=2000)


def _weekly(request, db):
    from aria.steward.weekly.service import WeeklyImprovement
    return getattr(request.app.state, 'weekly_improvement', None) or WeeklyImprovement(db)


def _weekly_json(value):
    from datetime import datetime
    from bson import ObjectId
    if isinstance(value, dict):
        return {k: _weekly_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_weekly_json(v) for v in value]
    if isinstance(value, (datetime, ObjectId)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    return value


@router.get('/weekly')
async def weekly_status(request: Request, db=Depends(get_db)):
    return _weekly_json(await _weekly(request, db).status())


@router.get('/runs')
async def weekly_runs(db=Depends(get_db), limit: int = 20):
    rows = await db.improver_runs.find({'schema_version': 2}, {'report': 0, 'events': 0}).sort('created_at', -1).limit(min(100, max(1, limit))).to_list(length=min(100, max(1, limit)))
    return _weekly_json(rows)


@router.get('/runs/{run_id}')
async def weekly_run(run_id: str, db=Depends(get_db)):
    row = await db.improver_runs.find_one({'_id': run_id, 'schema_version': 2})
    if not row:
        raise HTTPException(404, 'Weekly run not found')
    return _weekly_json(row)


@router.get('/runs/{run_id}/report')
async def weekly_report(run_id: str, db=Depends(get_db)):
    row = await weekly_run(run_id, db)
    return {'report': row.get('report'), 'publication': row.get('publication'), 'notification': row.get('notification')}


@router.post('/trigger', dependencies=[Depends(require_admin)])
async def trigger_weekly(body: WeeklyTrigger, request: Request, db=Depends(get_db)):
    try:
        return _weekly_json(await _weekly(request, db).trigger(body.policy_id, report_only=body.report_only))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))


@router.post('/runs/{run_id}/cancel', dependencies=[Depends(require_admin)])
async def cancel_weekly(run_id: str, db=Depends(get_db)):
    result = await db.improver_runs.update_one({'_id': run_id, 'active_slot': 'platform'}, {'$set': {'cancel_requested': True}})
    if not result.matched_count:
        raise HTTPException(409, 'Run is not active')
    return {'cancel_requested': True}


@router.get('/findings')
async def weekly_findings(db=Depends(get_db), limit: int = 50):
    limit = min(100, max(1, limit))
    return _weekly_json(await db.improvement_findings.find({}).sort('last_seen', -1).limit(limit).to_list(length=limit))


@router.post('/findings/{finding_id}/disposition', dependencies=[Depends(require_admin)])
async def set_weekly_disposition(finding_id: str, body: FindingDisposition, db=Depends(get_db)):
    result = await db.improvement_findings.update_one({'_id': finding_id, 'watch.active': {'$ne': True},
        'disposition': {'$nin': ['active', 'merged']}}, {'$set': {'disposition': body.status, 'feedback': body.reason}})
    if not result.matched_count:
        raise HTTPException(409, 'Finding missing or currently applying/watching')
    return {'updated': True}
