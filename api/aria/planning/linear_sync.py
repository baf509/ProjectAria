"""
ARIA - Linear sync + backlog reconciliation (Coherence C3)

Purpose: stop the backlog from silently accumulating. Periodically pull open
Linear issues for the per-project OPT-IN map (settings.linear_project_map),
mirror them into `tasks` as a read cache (source.type="import", Linear stays
authoritative), and run an LLM judge over each with local evidence (project
activity, machine_scan memories, git state, vault docs): clearly-implemented
tickets are auto-resolved in Linear (logged + commented + reversible);
plausibly-done ones surface a proposed disposition for a one-tap confirm;
everything else is left open. Keep/kill/do-now actions live in the C4 cockpit
routes (api/routes/linear.py) and write back to Linear.

Related Spec Sections:
- COHERENCE_DESIGN.md C3 (Linear Reconciliation)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.core.prompts import load_prompt
from aria.planning.service import PlanningService, _content_hash

logger = logging.getLogger(__name__)

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# Don't re-judge an issue more often than this; a human "keep" pauses judging
# for the longer window.
_REJUDGE_HOURS = 24
_KEEP_PAUSE_DAYS = 7


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LinearClient:
    """Thin GraphQL client over the Linear API. Server-internal; the API key
    comes from settings and is never logged."""

    def __init__(self, api_key: Optional[str] = None, *, timeout: float = 20.0):
        self.api_key = api_key or settings.linear_api_key
        self.timeout = timeout

    async def _gql(self, query: str, variables: Optional[dict] = None) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                LINEAR_GRAPHQL_URL,
                headers={"Authorization": self.api_key, "Content-Type": "application/json"},
                json={"query": query, "variables": variables or {}},
            )
            resp.raise_for_status()
            body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"Linear GraphQL error: {body['errors']}")
        return body.get("data") or {}

    async def list_open_issues(self, project_id: str) -> list[dict]:
        data = await self._gql(
            """
            query($projectId: String!) {
              project(id: $projectId) {
                issues(
                  first: 100,
                  filter: {state: {type: {nin: ["completed", "canceled"]}}}
                ) {
                  nodes {
                    id identifier title description url createdAt updatedAt
                    state { id name type }
                    team { id }
                  }
                }
              }
            }
            """,
            {"projectId": project_id},
        )
        return ((data.get("project") or {}).get("issues") or {}).get("nodes") or []

    async def resolve_issue(self, issue_id: str) -> bool:
        """Move an issue to its team's first `completed`-type state. Marks
        done — never deletes; Linear keeps full history so this is
        reversible."""
        data = await self._gql(
            """
            query($id: String!) {
              issue(id: $id) {
                id
                team { id states(first: 50) { nodes { id name type } } }
              }
            }
            """,
            {"id": issue_id},
        )
        states = (
            ((data.get("issue") or {}).get("team") or {}).get("states") or {}
        ).get("nodes") or []
        done = next((s for s in states if s.get("type") == "completed"), None)
        if not done:
            raise RuntimeError(f"no completed-type state found for issue {issue_id}")
        out = await self._gql(
            """
            mutation($id: String!, $stateId: String!) {
              issueUpdate(id: $id, input: {stateId: $stateId}) { success }
            }
            """,
            {"id": issue_id, "stateId": done["id"]},
        )
        return bool((out.get("issueUpdate") or {}).get("success"))

    async def comment(self, issue_id: str, body: str) -> bool:
        out = await self._gql(
            """
            mutation($id: String!, $body: String!) {
              commentCreate(input: {issueId: $id, body: $body}) { success }
            }
            """,
            {"id": issue_id, "body": body},
        )
        return bool((out.get("commentCreate") or {}).get("success"))

    async def create_issue(
        self, project_id: str, title: str, description: str = ""
    ) -> dict:
        data = await self._gql(
            """
            query($projectId: String!) {
              project(id: $projectId) { id teams(first: 1) { nodes { id } } }
            }
            """,
            {"projectId": project_id},
        )
        teams = ((data.get("project") or {}).get("teams") or {}).get("nodes") or []
        if not teams:
            raise RuntimeError(f"no team found for Linear project {project_id}")
        out = await self._gql(
            """
            mutation($input: IssueCreateInput!) {
              issueCreate(input: $input) {
                success
                issue { id identifier url }
              }
            }
            """,
            {
                "input": {
                    "teamId": teams[0]["id"],
                    "projectId": project_id,
                    "title": title,
                    "description": description,
                }
            },
        )
        created = out.get("issueCreate") or {}
        if not created.get("success"):
            raise RuntimeError("Linear issueCreate did not succeed")
        return created.get("issue") or {}


class LinearSyncWorker:
    """Periodic sync + reconciliation over the mapped Linear projects."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        notification_service,
        *,
        client: Optional[LinearClient] = None,
        interval_minutes: Optional[int] = None,
    ):
        self.db = db
        self.notifier = notification_service
        self.service = PlanningService(db)
        self.client = client or LinearClient()
        self.interval = (interval_minutes or settings.linear_sync_interval_minutes) * 60
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        logger.info("Linear sync worker started (interval=%ss)", self.interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("Linear sync tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ tick

    async def tick(self) -> dict:
        totals = {"mirrored": 0, "auto_resolved": 0, "proposed": 0, "closed_upstream": 0}
        for aria_slug, linear_pid in (settings.linear_project_map or {}).items():
            project = await self.service.get_project_by_slug(aria_slug)
            try:
                issues = await self.client.list_open_issues(linear_pid)
            except Exception as exc:
                logger.warning("Linear list failed for %s: %s", aria_slug, exc)
                continue
            open_ids = set()
            for issue in issues:
                open_ids.add(issue["id"])
                await self._mirror_issue(issue, project)
                totals["mirrored"] += 1
            totals["closed_upstream"] += await self._close_absent(linear_pid, open_ids)
            for issue in issues:
                outcome = await self._reconcile_issue(issue, project)
                if outcome in totals:
                    totals[outcome] += 1
        return totals

    async def _mirror_issue(self, issue: dict, project) -> None:
        now = _utcnow()
        ref = {
            "tracker": "linear",
            "id": issue["id"],
            "identifier": issue.get("identifier"),
            "url": issue.get("url"),
            "state": (issue.get("state") or {}).get("name"),
            "state_type": (issue.get("state") or {}).get("type"),
        }
        await self.db.tasks.update_one(
            {"external_ref.tracker": "linear", "external_ref.id": issue["id"]},
            {
                "$set": {
                    "title": issue.get("title") or "(untitled)",
                    "notes": (issue.get("description") or "")[:2000],
                    "external_ref": ref,
                    "project_id": getattr(project, "id", None),
                    "updated_at": now,
                    "external_updated_at": issue.get("updatedAt"),
                },
                "$setOnInsert": {
                    "status": "active",
                    "due_at": None,
                    "tags": ["linear"],
                    "source": {"type": "import"},
                    "content_hash": _content_hash(issue.get("title") or issue["id"]),
                    "created_at": now,
                    "completed_at": None,
                },
            },
            upsert=True,
        )

    async def _close_absent(self, linear_pid: str, open_ids: set[str]) -> int:
        """A mirrored task whose Linear issue is no longer open was resolved
        upstream — mark the read-cache copy done."""
        res = await self.db.tasks.update_many(
            {
                "external_ref.tracker": "linear",
                "external_ref.id": {"$nin": list(open_ids)},
                "status": {"$in": ["proposed", "active"]},
            },
            {
                "$set": {
                    "status": "done",
                    "completed_at": _utcnow(),
                    "updated_at": _utcnow(),
                }
            },
        )
        return res.modified_count

    # ----------------------------------------------------------- reconcile

    async def _reconcile_issue(self, issue: dict, project) -> Optional[str]:
        task = await self.db.tasks.find_one(
            {"external_ref.tracker": "linear", "external_ref.id": issue["id"]}
        )
        if not task or task.get("status") not in ("proposed", "active"):
            return None
        rec = task.get("reconcile") or {}
        now = _utcnow()
        kept_at = rec.get("kept_at")
        if kept_at is not None:
            if kept_at.tzinfo is None:
                kept_at = kept_at.replace(tzinfo=timezone.utc)
            if now - kept_at < timedelta(days=_KEEP_PAUSE_DAYS):
                return None
        judged_at = rec.get("judged_at")
        if judged_at is not None:
            if judged_at.tzinfo is None:
                judged_at = judged_at.replace(tzinfo=timezone.utc)
            if now - judged_at < timedelta(hours=_REJUDGE_HOURS):
                return None
        if task.get("proposed_disposition"):
            return None  # already awaiting a human decision

        evidence = await self._gather_evidence(project)
        verdict = await self._judge(issue, evidence)
        await self.db.tasks.update_one(
            {"_id": task["_id"]},
            {"$set": {"reconcile": {**rec, "judged_at": now, **(verdict or {})}}},
        )
        if not verdict:
            return None

        confidence = float(verdict.get("confidence") or 0.0)
        implemented = bool(verdict.get("implemented"))
        cited = str(verdict.get("evidence") or "")[:800]
        ident = issue.get("identifier") or issue["id"]

        if (
            implemented
            and settings.linear_reconcile_auto_resolve
            and confidence >= settings.linear_reconcile_auto_confidence
            and cited
        ):
            try:
                await self.client.resolve_issue(issue["id"])
                await self.client.comment(
                    issue["id"],
                    f"Auto-resolved by ARIA (confidence {confidence:.2f}): "
                    f"{cited}\n\nReopen if this is wrong — nothing was deleted.",
                )
            except Exception as exc:
                logger.warning("auto-resolve failed for %s: %s", ident, exc)
                return None
            await self.db.tasks.update_one(
                {"_id": task["_id"]},
                {"$set": {"status": "done", "completed_at": now, "updated_at": now}},
            )
            await self.notifier.notify(
                source="linear:reconcile",
                event_type="auto_resolved",
                detail=f"{ident} '{issue.get('title')}' auto-resolved as already "
                f"implemented ({confidence:.2f}): {cited}",
                cooldown_seconds=1,
                project_path=getattr(project, "path", None),
            )
            logger.info("linear: auto-resolved %s (%.2f)", ident, confidence)
            return "auto_resolved"

        if implemented and confidence >= settings.linear_reconcile_propose_confidence:
            await self.db.tasks.update_one(
                {"_id": task["_id"]},
                {
                    "$set": {
                        "proposed_disposition": {
                            "action": "resolve",
                            "confidence": confidence,
                            "evidence": cited,
                            "proposed_at": now,
                        },
                        "updated_at": now,
                    }
                },
            )
            await self.notifier.notify(
                source="linear:reconcile",
                event_type="proposed",
                detail=f"{ident} '{issue.get('title')}' looks already implemented "
                f"({confidence:.2f}): {cited} — confirm to resolve.",
                cooldown_seconds=1,
                project_path=getattr(project, "path", None),
            )
            return "proposed"
        return None

    async def _gather_evidence(self, project) -> str:
        parts: list[str] = []
        if project is not None:
            git = project.git or {}
            if git:
                parts.append(
                    f"- git: branch {git.get('branch')}, last commit "
                    f"'{git.get('last_commit_subject')}' at {git.get('last_commit_at')}"
                )
            for act in (project.recent_activity or [])[-10:]:
                parts.append(f"- activity ({act.source}): {act.note}")
            if project.path:
                cursor = self.db.memories.find(
                    {"source.type": "machine_scan", "source.repo": project.path}
                ).sort("created_at", -1)
                for m in await cursor.to_list(length=8):
                    parts.append(f"- repo change: {str(m.get('content'))[:300]}")
                vault = os.path.join(
                    settings.obsidian_vault_path,
                    os.path.basename(project.path.rstrip("/")),
                )
                docs = await asyncio.to_thread(self._list_vault_docs, vault)
                for d in docs:
                    parts.append(f"- vault doc: {d}")
        return "\n".join(parts) if parts else "(no evidence found)"

    @staticmethod
    def _list_vault_docs(folder: str, limit: int = 20) -> list[str]:
        out: list[str] = []
        try:
            for root, _dirs, files in os.walk(folder):
                for f in files:
                    if f.endswith(".md"):
                        out.append(os.path.relpath(os.path.join(root, f), folder))
                        if len(out) >= limit:
                            return out
        except OSError:
            pass
        return out

    async def _judge(self, issue: dict, evidence: str) -> Optional[dict]:
        """LLM verdict: is this ticket already implemented? Mirrors the
        TaskExtractor call path (Claude runner when available, else the local
        ambient backend — no cloud fallback exists on this host)."""
        prompt = load_prompt(
            "linear_reconcile",
            title=issue.get("title") or "",
            description=(issue.get("description") or "")[:2000],
            evidence=evidence,
        )
        try:
            from aria.core.claude_runner import ClaudeRunner
            response: Optional[str] = None
            if settings.use_claude_runner and ClaudeRunner.is_available():
                runner = ClaudeRunner(
                    timeout_seconds=settings.claude_runner_timeout_seconds
                )
                response = await runner.run(prompt)
            if not response:
                from aria.llm.base import Message
                from aria.llm.manager import llm_manager
                adapter = llm_manager.get_adapter(
                    settings.planning_ambient_backend, settings.planning_ambient_model
                )
                response, _, _usage = await adapter.complete(
                    messages=[Message(role="user", content=prompt)],
                    temperature=0.1,
                    max_tokens=512,
                )
        except Exception as exc:
            logger.warning("linear judge LLM call failed: %s", exc)
            return None
        try:
            cleaned = (response or "").strip()
            if cleaned.startswith("```"):
                cleaned = "\n".join(
                    l for l in cleaned.splitlines() if not l.strip().startswith("```")
                ).strip()
            data = json.loads(cleaned)
            return {
                "implemented": bool(data.get("implemented")),
                "confidence": float(data.get("confidence") or 0.0),
                "evidence": str(data.get("evidence") or ""),
            }
        except Exception:
            logger.warning("linear judge returned unparseable verdict: %r", (response or "")[:300])
            return None
