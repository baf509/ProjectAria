"""Tests for the C3 Linear sync + reconciliation worker (faked Linear client
and judge — no network, no LLM)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.config import settings
from aria.planning.linear_sync import LinearSyncWorker


def _issue(iid="iss-1", title="Build the widget", state_type="started"):
    return {
        "id": iid,
        "identifier": "WAR-1",
        "title": title,
        "description": "Make it so",
        "url": f"https://linear.app/x/{iid}",
        "createdAt": "2026-07-01T00:00:00Z",
        "updatedAt": "2026-07-20T00:00:00Z",
        "state": {"id": "s1", "name": "In Progress", "type": state_type},
        "team": {"id": "t1"},
    }


class _FakeTasks:
    def __init__(self):
        self.docs: dict[str, dict] = {}  # keyed by external id for convenience

    def _match(self, doc, q):
        for k, v in q.items():
            if k == "external_ref.tracker":
                if (doc.get("external_ref") or {}).get("tracker") != v:
                    return False
            elif k == "external_ref.id":
                ext = (doc.get("external_ref") or {}).get("id")
                if isinstance(v, dict) and "$nin" in v:
                    if ext in v["$nin"]:
                        return False
                elif ext != v:
                    return False
            elif k == "status":
                if isinstance(v, dict) and "$in" in v:
                    if doc.get("status") not in v["$in"]:
                        return False
                elif doc.get("status") != v:
                    return False
            elif k == "_id":
                if doc.get("_id") != v:
                    return False
        return True

    async def find_one(self, q):
        for doc in self.docs.values():
            if self._match(doc, q):
                return doc
        return None

    async def update_one(self, q, update, upsert=False):
        for doc in self.docs.values():
            if self._match(doc, q):
                self._apply(doc, update)
                return MagicMock(matched_count=1, modified_count=1)
        if upsert:
            doc = {"_id": f"t{len(self.docs)}"}
            self._apply(doc, update, is_insert=True)
            key = (doc.get("external_ref") or {}).get("id") or doc["_id"]
            self.docs[key] = doc
            return MagicMock(matched_count=0, modified_count=0, upserted_id=doc["_id"])
        return MagicMock(matched_count=0, modified_count=0)

    async def update_many(self, q, update):
        n = 0
        for doc in self.docs.values():
            if self._match(doc, q):
                self._apply(doc, update)
                n += 1
        return MagicMock(modified_count=n)

    @staticmethod
    def _apply(doc, update, is_insert=False):
        for k, v in (update.get("$set") or {}).items():
            if "." in k:
                head, tail = k.split(".", 1)
                doc.setdefault(head, {})[tail] = v
            else:
                doc[k] = v
        if is_insert:
            for k, v in (update.get("$setOnInsert") or {}).items():
                doc.setdefault(k, v)
        for k in (update.get("$unset") or {}):
            doc.pop(k, None)


def _make_worker(issues=None):
    db = MagicMock()
    db.tasks = _FakeTasks()
    mem_cursor = MagicMock()
    mem_cursor.sort.return_value = mem_cursor
    mem_cursor.to_list = AsyncMock(return_value=[])
    db.memories.find = MagicMock(return_value=mem_cursor)
    notifier = MagicMock()
    notifier.notify = AsyncMock(return_value={"queued": True})
    client = MagicMock()
    client.list_open_issues = AsyncMock(return_value=issues or [])
    client.resolve_issue = AsyncMock(return_value=True)
    client.comment = AsyncMock(return_value=True)
    worker = LinearSyncWorker(db, notifier, client=client, interval_minutes=30)
    project = MagicMock()
    project.id = "P1"
    project.path = "/tmp/demo"
    project.git = {"branch": "master", "last_commit_subject": "feat: widget"}
    project.recent_activity = []
    worker.service = MagicMock()
    worker.service.get_project_by_slug = AsyncMock(return_value=project)
    return worker, db, client, notifier


def _map_demo():
    return patch.object(settings, "linear_project_map", {"demo": "lin-proj-1"})


@pytest.mark.asyncio
async def test_tick_mirrors_open_issues_as_import_tasks():
    worker, db, client, _ = _make_worker([_issue()])
    worker._judge = AsyncMock(return_value=None)
    with _map_demo():
        totals = await worker.tick()
    assert totals["mirrored"] == 1
    doc = db.tasks.docs["iss-1"]
    assert doc["title"] == "Build the widget"
    assert doc["status"] == "active"
    assert doc["source"] == {"type": "import"}
    assert doc["external_ref"]["identifier"] == "WAR-1"
    assert doc["project_id"] == "P1"


@pytest.mark.asyncio
async def test_tick_marks_upstream_closed_issues_done():
    worker, db, client, _ = _make_worker([_issue("iss-1")])
    worker._judge = AsyncMock(return_value=None)
    with _map_demo():
        await worker.tick()
        client.list_open_issues = AsyncMock(return_value=[])  # closed upstream
        totals = await worker.tick()
    assert totals["closed_upstream"] == 1
    assert db.tasks.docs["iss-1"]["status"] == "done"


@pytest.mark.asyncio
async def test_high_confidence_auto_resolves_with_comment_and_alert():
    worker, db, client, notifier = _make_worker([_issue()])
    worker._judge = AsyncMock(
        return_value={"implemented": True, "confidence": 0.95, "evidence": "commit feat: widget"}
    )
    with _map_demo(), patch.object(settings, "linear_reconcile_auto_resolve", True):
        totals = await worker.tick()
    assert totals["auto_resolved"] == 1
    client.resolve_issue.assert_awaited_once_with("iss-1")
    client.comment.assert_awaited_once()
    assert db.tasks.docs["iss-1"]["status"] == "done"
    assert notifier.notify.await_args.kwargs["source"] == "linear:reconcile"
    assert notifier.notify.await_args.kwargs["event_type"] == "auto_resolved"


@pytest.mark.asyncio
async def test_mid_confidence_proposes_instead_of_resolving():
    worker, db, client, notifier = _make_worker([_issue()])
    worker._judge = AsyncMock(
        return_value={"implemented": True, "confidence": 0.8, "evidence": "design doc exists"}
    )
    with _map_demo():
        totals = await worker.tick()
    assert totals["proposed"] == 1
    client.resolve_issue.assert_not_awaited()
    doc = db.tasks.docs["iss-1"]
    assert doc["status"] == "active"
    assert doc["proposed_disposition"]["action"] == "resolve"
    assert notifier.notify.await_args.kwargs["event_type"] == "proposed"


@pytest.mark.asyncio
async def test_low_confidence_leaves_ticket_open():
    worker, db, client, notifier = _make_worker([_issue()])
    worker._judge = AsyncMock(
        return_value={"implemented": False, "confidence": 0.3, "evidence": ""}
    )
    with _map_demo():
        totals = await worker.tick()
    assert totals["auto_resolved"] == 0 and totals["proposed"] == 0
    assert db.tasks.docs["iss-1"]["status"] == "active"
    assert "proposed_disposition" not in db.tasks.docs["iss-1"]
    notifier.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_resolve_without_cited_evidence_does_not_fire():
    """The threshold gate requires CITED evidence, not just a confident bool."""
    worker, db, client, _ = _make_worker([_issue()])
    worker._judge = AsyncMock(
        return_value={"implemented": True, "confidence": 0.99, "evidence": ""}
    )
    with _map_demo():
        totals = await worker.tick()
    assert totals["auto_resolved"] == 0
    client.resolve_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_kept_issue_is_not_rejudged():
    worker, db, client, _ = _make_worker([_issue()])
    worker._judge = AsyncMock(
        return_value={"implemented": True, "confidence": 0.95, "evidence": "x"}
    )
    with _map_demo():
        await worker.tick()  # mirrors + would auto-resolve...
    # reset to open and mark kept
    doc = db.tasks.docs["iss-1"]
    doc["status"] = "active"
    doc["reconcile"] = {"kept_at": datetime.now(timezone.utc)}
    worker._judge.reset_mock()
    with _map_demo():
        await worker.tick()
    worker._judge.assert_not_awaited()


@pytest.mark.asyncio
async def test_recently_judged_issue_is_not_rejudged():
    worker, db, client, _ = _make_worker([_issue()])
    worker._judge = AsyncMock(return_value=None)
    with _map_demo():
        await worker.tick()
        worker._judge.reset_mock()
        await worker.tick()  # judged_at was just stamped
    worker._judge.assert_not_awaited()
