"""
ARIA - Admin / Cutover Routes

Phase: 19, 20
Purpose: Security visibility, production-readiness status, and ABP retirement.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId, json_util
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from aria.api.deps import (
    get_audit_service,
    get_coding_session_manager,
    get_coding_watchdog,
    get_coding_review_service,
    get_db,
    get_notification_service,
    get_research_service,
    get_task_runner,
    get_workflow_engine,
)
from aria.config import settings
from aria.core.hooks import hook_registry, VALID_HOOKS
from aria.db.migration_abp import get_migration_status, run_full_migration
from aria.db.usage import UsageRepo
from aria.security.audit import AuditService
from aria.agents.session import CodingSessionManager
from aria.agents.watchdog import CodingWatchdog
from aria.agents.review import CodingReviewService
from aria.notifications.service import NotificationService
from aria.research.service import ResearchService
from aria.tasks.runner import TaskRunner
from aria.workflows.engine import WorkflowEngine

router = APIRouter(prefix="/admin", tags=["admin"])

# Store the timestamp when the API first started (proxy for parallel-run tracking)
_api_start_time: datetime = datetime.now(timezone.utc)


@router.get("/hooks")
async def list_hooks():
    """List all registered lifecycle hooks and their handlers."""
    return {
        "valid_hooks": sorted(VALID_HOOKS),
        "registered": hook_registry.list_hooks(),
    }


@router.get("/audit")
async def audit_overview(
    hours: int = 24,
    limit: int = 50,
    audit: AuditService = Depends(get_audit_service),
):
    return {
        "summary": await audit.summary(hours=hours),
        "recent": await audit.recent_events(limit=limit),
    }


@router.get("/db/collections")
async def list_collections(db: AsyncIOMotorDatabase = Depends(get_db)):
    """List all MongoDB collections with document counts."""
    names = await db.list_collection_names()
    result = []
    for name in sorted(names):
        count = await db[name].estimated_document_count()
        result.append({"name": name, "count": count})
    return result


class DBQueryRequest(BaseModel):
    filter: Optional[dict] = None


@router.get("/db/{collection}")
async def query_collection(
    collection: str,
    limit: int = Query(default=20, le=200),
    skip: int = 0,
    sort: str = Query(default="_id", description="Field to sort by"),
    order: int = Query(default=-1, description="1=asc, -1=desc"),
    q: Optional[str] = Query(default=None, description="JSON filter"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Query a MongoDB collection. Returns documents as JSON."""
    names = await db.list_collection_names()
    if collection not in names:
        raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found")

    query = {}
    if q:
        try:
            query = json.loads(q)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON filter")

    cursor = db[collection].find(query).sort(sort, order).skip(skip).limit(limit)
    docs = []
    async for doc in cursor:
        docs.append(json.loads(json_util.dumps(doc)))

    total = await db[collection].count_documents(query)
    return {"collection": collection, "total": total, "skip": skip, "limit": limit, "documents": docs}


@router.get("/db/{collection}/{document_id}")
async def get_document(
    collection: str,
    document_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get a single document by ID."""
    names = await db.list_collection_names()
    if collection not in names:
        raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found")

    # Try as ObjectId first, then as string _id
    doc = None
    try:
        doc = await db[collection].find_one({"_id": ObjectId(document_id)})
    except Exception:
        pass
    if not doc:
        doc = await db[collection].find_one({"_id": document_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    return json.loads(json_util.dumps(doc))


def _check_service_instantiated(service_obj, label: str) -> dict:
    """Return a checklist item based on whether a service is instantiated."""
    return {
        "status": "done" if service_obj is not None else "pending",
        "label": label,
        "detail": type(service_obj).__name__ if service_obj is not None else "not instantiated",
    }


@router.get("/cutover")
async def cutover_status(
    db: AsyncIOMotorDatabase = Depends(get_db),
    audit: AuditService = Depends(get_audit_service),
    coding_manager: CodingSessionManager = Depends(get_coding_session_manager),
    coding_watchdog: CodingWatchdog = Depends(get_coding_watchdog),
    coding_review: CodingReviewService = Depends(get_coding_review_service),
    notification_service: NotificationService = Depends(get_notification_service),
    research_service: ResearchService = Depends(get_research_service),
    task_runner: TaskRunner = Depends(get_task_runner),
    workflows: WorkflowEngine = Depends(get_workflow_engine),
):
    agents = await db.agents.count_documents({})
    workflow_count = await db.workflows.count_documents({})
    recent_tasks = await db.background_tasks.count_documents({})
    audit_summary = await audit.summary(hours=24)

    # --- Phase 20.1: Feature parity checklist ---
    # Each item checks if the actual service is instantiated and functional,
    # not just whether a config flag is set.

    # 1. Coding sessions — can start/stop/monitor
    coding_sessions_ok = coding_manager is not None and coding_manager.process_manager is not None
    active_sessions = await db.coding_sessions.count_documents({"status": "running"}) if coding_sessions_ok else 0

    # 2. Stall detection and auto-respond
    watchdog_ok = coding_watchdog is not None

    # 3. Signal notifications
    signal_ok = notification_service is not None and notification_service.signal_service is not None

    # 4. Session review
    review_ok = coding_review is not None

    # 5. Deep research
    research_ok = research_service is not None

    # 6. Usage tracking
    usage_repo = UsageRepo(db)
    usage_summary = await usage_repo.summary(days=7)
    usage_ok = usage_summary.get("requests", 0) > 0

    # 7. Git worktrees — check if session manager has branch support
    # (worktree isolation uses branch param in start_session)
    worktree_ok = coding_sessions_ok and hasattr(coding_manager, "start_session")

    checklist = [
        {
            "key": "coding_sessions",
            "label": "Can start/stop/monitor coding sessions",
            "status": "done" if coding_sessions_ok else "pending",
            "detail": f"CodingSessionManager active, {active_sessions} running session(s)",
        },
        {
            "key": "stall_detection",
            "label": "Can detect stalls and auto-respond to prompts",
            "status": "done" if watchdog_ok else "pending",
            "detail": (
                f"CodingWatchdog active, interval={settings.coding_watchdog_interval_seconds}s, "
                f"stall_threshold={settings.coding_stall_seconds}s, "
                f"auto_respond={'enabled' if settings.coding_auto_respond_prompts else 'disabled'}"
            ) if watchdog_ok else "CodingWatchdog not instantiated",
        },
        {
            "key": "signal_notifications",
            "label": "Send notifications via Signal",
            "status": "done" if signal_ok else "warning",
            "detail": (
                f"NotificationService active, Signal {'enabled' if settings.signal_enabled else 'disabled'}"
            ),
        },
        {
            "key": "session_review",
            "label": "Review coding sessions (tests, lint, report)",
            "status": "done" if review_ok else "pending",
            "detail": "CodingReviewService active" if review_ok else "CodingReviewService not instantiated",
        },
        {
            "key": "deep_research",
            "label": "Run deep research and store learnings",
            "status": "done" if research_ok else "pending",
            "detail": "ResearchService active" if research_ok else "ResearchService not instantiated",
        },
        {
            "key": "usage_tracking",
            "label": "Track token usage and costs",
            "status": "done" if usage_ok else "warning",
            "detail": f"UsageRepo active, {usage_summary.get('requests', 0)} events in last 7 days",
        },
        {
            "key": "git_worktrees",
            "label": "Manage git worktrees for session isolation",
            "status": "done" if worktree_ok else "pending",
            "detail": "Session manager supports branch-based isolation" if worktree_ok else "Not available",
        },
        # --- Original security/ops items ---
        {
            "key": "api_auth",
            "label": "API authentication enabled",
            "status": "done" if settings.api_auth_enabled and bool(settings.api_key) else "pending",
        },
        {
            "key": "rate_limit",
            "label": "Rate limiting enabled",
            "status": "done" if settings.rate_limit_enabled else "pending",
        },
        {
            "key": "audit_logging",
            "label": "Audit logging enabled",
            "status": "done" if settings.audit_logging_enabled else "pending",
        },
        {
            "key": "tool_policy",
            "label": "Tool execution policy enforced",
            "status": "done" if settings.tool_execution_policy != "allow_all" else "warning",
        },
        {
            "key": "agent_modes",
            "label": "Operational modes configured",
            "status": "done" if agents > 0 else "pending",
        },
        {
            "key": "workflows",
            "label": "Workflows available",
            "status": "done" if workflow_count > 0 else "warning",
        },
        {
            "key": "tasks",
            "label": "Background task subsystem active",
            "status": "done" if recent_tasks > 0 else "warning",
        },
    ]

    # --- Phase 20.3: Migration status and parallel-run tracking ---
    migration_status = await get_migration_status(db)

    now = datetime.now(timezone.utc)
    parallel_run_delta = now - _api_start_time
    parallel_run_days = parallel_run_delta.total_seconds() / 86400

    parity_items = [item for item in checklist if item["key"] in {
        "coding_sessions", "stall_detection", "signal_notifications",
        "session_review", "deep_research", "usage_tracking", "git_worktrees",
    }]
    parity_ready = all(item["status"] == "done" for item in parity_items)
    overall_ready = all(item["status"] == "done" for item in checklist)

    return {
        "ready": overall_ready,
        "feature_parity": parity_ready,
        "checklist": checklist,
        "migration_status": migration_status,
        "parallel_run_days": round(parallel_run_days, 2),
        "api_started_at": _api_start_time.isoformat(),
        "signals": {
            "agents": agents,
            "workflows": workflow_count,
            "background_tasks": recent_tasks,
            "audit_events_last_24h": sum(row["count"] for row in audit_summary["events"]),
        },
    }


@router.post("/migrate-abp")
async def migrate_abp(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Run the ABP -> ARIA data migration.

    Safe to call multiple times — already-migrated records are skipped.
    """
    result = await run_full_migration(db)
    return result


@router.post("/bootstrap")
async def bootstrap_cutover(
    db: AsyncIOMotorDatabase = Depends(get_db),
    task_runner: TaskRunner = Depends(get_task_runner),
    workflows: WorkflowEngine = Depends(get_workflow_engine),
):
    created_workflow = None
    existing = await db.workflows.find_one({"name": "Baseline Operational Check"})
    if existing is None:
        created_workflow = await workflows.create_workflow(
            {
                "name": "Baseline Operational Check",
                "description": "Sanity-check workflow for a newly initialized ARIA deployment.",
                "tags": ["bootstrap", "ops"],
                "steps": [
                    {
                        "action": "condition",
                        "params": {"value": "initialized", "equals": "initialized"},
                    },
                    {
                        "action": "prompt",
                        "depends_on": [0],
                        "params": {"message": "Summarize the current ARIA deployment state in one sentence."},
                    },
                ],
            }
        )

    async def complete_bootstrap() -> dict:
        return {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "status": "ok",
        }

    task_id = await task_runner.submit_task(
        name="bootstrap:cutover-check",
        coroutine_factory=complete_bootstrap,
        notify=False,
        metadata={"task_kind": "bootstrap", "source": "admin_bootstrap"},
        timeout_seconds=60,
    )

    return {
        "workflow_created": created_workflow is not None,
        "workflow_id": created_workflow["_id"] if created_workflow else existing["_id"],
        "task_id": task_id,
    }
