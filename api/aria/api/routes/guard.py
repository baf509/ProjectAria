"""
ARIA - Guard Routes

Purpose: Operator surface for the guard — is it healthy, what has it blocked,
what can be rolled back, and (behind the admin key) merge a session's work.

Read endpoints are deliberately cheap and side-effect-free so the cockpit can
poll them. The only mutating endpoints are the ones a human or the steward has
to be able to reach: checkpoint, rollback, and merge.

**Merging is the one action gated by ADMIN_KEY.** Autonomy A ≤ 2 means a merge is
Ben's decision (`APPLY <id>`), and the global `API_KEY` is readable by anything
running as ben — including a coding agent — so it cannot be what authorises the
irreversible step. `require_admin` lives in this file for now; see the INTEGRATION
SPEC about moving it to `api/deps.py` alongside the other key-split routes
(`PUT /agents`, `set_llm_route`, model start/stop, killswitch deactivate).
"""

import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_db, require_admin
from aria.config import settings
from aria.guard import policy as guard_policy
from aria.guard.gitguard import (
    GUARD_CHECKPOINTS_COLLECTION,
    GUARD_SESSIONS_COLLECTION,
    GuardGitError,
    get_git_guard,
)
from aria.guard.sandbox import preflight

logger = logging.getLogger(__name__)

router = APIRouter()




class CheckpointRequest(BaseModel):
    reason: str = Field(default="manual", description="Why this checkpoint was taken")


class RollbackRequest(BaseModel):
    to: str = Field(
        default="start",
        description="'start' for the pre-session tag, or a commit sha reachable "
                    "from the session branch",
    )


class MergeRequest(BaseModel):
    squash: bool = Field(default=True, description="Squash-merge (the default) vs --no-ff")


class AcceptPolicyRequest(BaseModel):
    hash: str = Field(description="The policy hash being blessed as expected")
    actor: str = Field(default="api")


@router.get("/guard/status")
async def guard_status(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Preflight, policy hash + tamper verdict, and what the guard has recorded."""
    policy = guard_policy.load_policy(force=True)
    verification = await guard_policy.verify_policy(db)

    counts: dict = {}
    try:
        counts = {
            "sessions": await db[GUARD_SESSIONS_COLLECTION].count_documents({}),
            "active_sessions": await db[GUARD_SESSIONS_COLLECTION].count_documents(
                {"status": "active"}
            ),
            "checkpoints": await db[GUARD_CHECKPOINTS_COLLECTION].count_documents({}),
            "events": await db[guard_policy.GUARD_EVENTS_COLLECTION].count_documents({}),
            "blocked_events": await db[guard_policy.GUARD_EVENTS_COLLECTION].count_documents(
                {"blocked": True}
            ),
        }
    except Exception as exc:  # noqa: BLE001 — status must answer even without Mongo
        logger.warning("guard: counts unavailable: %s", exc)
        counts = {"error": str(exc)}

    return {
        "enabled": settings.guard_enabled,
        "preflight": preflight(),
        "policy": {**policy.to_dict(), "verification": verification},
        "counts": counts,
        "mirror_root": get_git_guard(db).mirror_root,
    }


@router.get("/guard/events")
async def list_guard_events(
    limit: int = 100,
    session_id: Optional[str] = None,
    kind: Optional[str] = None,
    blocked_only: bool = False,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Newest-first guard events (blocked actions, tamper, checkpoints, merges)."""
    query: dict = {}
    if session_id:
        query["session_id"] = session_id
    if kind:
        query["kind"] = kind
    if blocked_only:
        query["blocked"] = True
    cursor = db[guard_policy.GUARD_EVENTS_COLLECTION].find(query).sort("at", -1).limit(
        max(1, min(limit, 1000))
    )
    events = await cursor.to_list(length=None)
    for event in events:
        event["_id"] = str(event.get("_id"))
    return {"count": len(events), "events": events}


@router.get("/guard/checkpoints")
async def list_checkpoints(
    session_id: Optional[str] = None,
    limit: int = 50,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Real commits, newest first — each one a `git reset --hard <sha>` target."""
    query = {"session_id": session_id} if session_id else {}
    cursor = db[GUARD_CHECKPOINTS_COLLECTION].find(query).sort("at", -1).limit(
        max(1, min(limit, 500))
    )
    rows = await cursor.to_list(length=None)
    for row in rows:
        row["_id"] = str(row.get("_id"))
    return {"count": len(rows), "checkpoints": rows}


@router.post("/guard/sessions/{session_id}/checkpoint")
async def checkpoint_session(
    session_id: str,
    body: CheckpointRequest = CheckpointRequest(),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Commit the session's current worktree state. No-op on a clean tree."""
    try:
        result = await get_git_guard(db).checkpoint(session_id, reason=body.reason)
    except GuardGitError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not result.get("ok") and result.get("reason", "").startswith("no guard session"):
        raise HTTPException(status_code=404, detail=result["reason"])
    return result


@router.post("/guard/sessions/{session_id}/rollback")
async def rollback_session(
    session_id: str,
    body: RollbackRequest = RollbackRequest(),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """`git reset --hard` inside the session's worktree only."""
    try:
        result = await get_git_guard(db).rollback(session_id, to=body.to)
    except GuardGitError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("reason", "rollback failed"))
    return result


@router.get("/guard/sessions/{session_id}/merge-gate")
async def session_merge_gate(
    session_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Run every gate check and return the verdict. Never merges.

    ⚠️ There is deliberately NO caller-supplied `check_command`. It existed as an
    unauthenticated GET query parameter for a few hours on 2026-08-15 and was a
    remote shell: the string went straight to `create_subprocess_shell` as `ben`
    with aria-api's whole environment — including ADMIN_KEY and API_KEY from
    .env — and the last 1500 bytes of output were echoed back in the verdict, so
    `?check_command=env` returned the admin key to anything that could reach the
    MCP surface. The gate's command now comes only from the project's own
    `check_command` or the configured default, which is the only version of this
    that a gate can safely have: the thing being verified must not choose the
    verification."""
    try:
        return await get_git_guard(db).merge_gate(session_id)
    except GuardGitError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/guard/sessions/{session_id}/merge", dependencies=[Depends(require_admin)])
async def merge_session(
    session_id: str,
    body: MergeRequest = MergeRequest(),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Squash-merge a session branch into its source branch (ADMIN_KEY).

    Refuses unless `merge-gate` passed against the branch's current head.
    """
    try:
        result = await get_git_guard(db).merge(session_id, squash=body.squash, actor="admin-api")
    except GuardGitError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("reason", "merge refused"))
    return result


@router.post("/guard/policy/accept", dependencies=[Depends(require_admin)])
async def accept_guard_policy(
    body: AcceptPolicyRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Bless a new policy hash after a deliberate human edit (ADMIN_KEY).

    The hash must be passed explicitly: accepting "whatever is on disk now"
    would let a caller who never read the file wave through someone else's edit.
    """
    current = guard_policy.policy_hash()
    if body.hash != current:
        raise HTTPException(
            status_code=409,
            detail=f"hash mismatch: the enforced policy is {current}, not {body.hash}",
        )
    return await guard_policy.accept_policy(db, current, actor=body.actor)
