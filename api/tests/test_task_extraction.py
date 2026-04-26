"""Tests for the ambient TaskExtractor.

Focuses on the parsing/dedup/private-skip logic. The LLM call is mocked.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

from aria.planning.extraction import TaskExtractor
from aria.planning.models import Project, ProjectActivity, Task, TaskSource


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conv_doc(messages: list[dict], private: bool = False) -> dict:
    return {
        "_id": ObjectId(),
        "private": private,
        "messages": messages,
    }


def _user_msg(content: str, msg_id: str | None = None) -> dict:
    return {
        "id": msg_id or str(ObjectId()),
        "role": "user",
        "content": content,
        "task_processed": False,
    }


# ----------------------------------------------------- response parsing


def test_parse_response_strips_markdown_fence():
    extractor = TaskExtractor.__new__(TaskExtractor)  # bypass __init__
    response = "```json\n" + json.dumps({"tasks": [{"title": "x", "confidence": 0.9}]}) + "\n```"
    parsed = extractor._parse_response(response)
    assert parsed["tasks"] == [{"title": "x", "confidence": 0.9}]
    assert parsed["project_updates"] == []
    assert parsed["new_projects"] == []


def test_parse_response_handles_missing_arrays():
    extractor = TaskExtractor.__new__(TaskExtractor)
    parsed = extractor._parse_response('{"tasks": []}')
    assert parsed == {"tasks": [], "project_updates": [], "new_projects": []}


def test_parse_response_rejects_non_object():
    extractor = TaskExtractor.__new__(TaskExtractor)
    with pytest.raises(Exception):
        extractor._parse_response('["not", "an", "object"]')


# --------------------------------------------------- private conversation


@pytest.mark.asyncio
async def test_skips_private_conversation():
    """Private conversations must not trigger any LLM call or insert anything."""
    extractor = TaskExtractor.__new__(TaskExtractor)
    extractor.db = None  # not touched
    counts = await extractor.extract_from_conversation(
        str(ObjectId()), private=True
    )
    assert counts == {
        "tasks_proposed": 0,
        "tasks_deduped": 0,
        "projects_updated": 0,
        "new_projects": 0,
    }


# ----------------------------------------------------------- _apply logic


class _FakeService:
    """Minimal stand-in for PlanningService used by _apply tests."""
    def __init__(self):
        self.created_tasks = []
        self.created_projects = []
        self.activities = []
        self.next_steps_set = []
        self._open_hashes: set[str] = set()
        self._existing_projects: dict[str, Project] = {}

    def with_open_task_hash(self, content_hash: str):
        self._open_hashes.add(content_hash)
        return self

    def with_existing_project(self, hint: str, project: Project):
        self._existing_projects[hint.lower()] = project
        return self

    async def find_open_task_by_hash(self, content_hash: str):
        if content_hash in self._open_hashes:
            return Task(
                id="existing",
                title="x",
                status="active",
                source=TaskSource(type="manual"),
                content_hash=content_hash,
                created_at=_now(),
                updated_at=_now(),
            )
        return None

    async def fuzzy_find_project(self, hint: str):
        return self._existing_projects.get(hint.lower())

    async def create_task(self, body, *, source=None):
        self.created_tasks.append((body, source))
        return Task(
            id=f"new-{len(self.created_tasks)}",
            title=body.title,
            status=body.status,
            source=source or TaskSource(type="manual"),
            content_hash="x" * 64,
            created_at=_now(),
            updated_at=_now(),
        )

    async def create_project(self, body):
        from aria.planning.service import _slugify
        proj = Project(
            id=f"proj-{len(self.created_projects)}",
            name=body.name,
            slug=_slugify(body.name),
            summary=body.summary,
            status="active",
            created_at=_now(),
            updated_at=_now(),
        )
        self.created_projects.append(proj)
        return proj

    async def append_project_activity(self, project_id, *, source, note):
        self.activities.append((project_id, source, note))
        return True

    async def set_project_next_steps(self, project_id, steps):
        self.next_steps_set.append((project_id, steps))
        return True


def _make_extractor_with_service(service: _FakeService) -> TaskExtractor:
    extractor = TaskExtractor.__new__(TaskExtractor)
    extractor.db = None
    extractor.service = service
    return extractor


@pytest.mark.asyncio
async def test_apply_skips_low_confidence_tasks():
    service = _FakeService()
    extractor = _make_extractor_with_service(service)
    payload = {
        "tasks": [
            {"title": "Vague idea", "confidence": 0.3},  # below threshold
            {"title": "Concrete task", "confidence": 0.9},
        ],
        "project_updates": [],
        "new_projects": [],
    }
    counts = await extractor._apply(payload, "conv1", ["m1"])
    assert counts["tasks_proposed"] == 1
    assert len(service.created_tasks) == 1
    assert service.created_tasks[0][0].title == "Concrete task"


@pytest.mark.asyncio
async def test_apply_dedups_against_open_tasks():
    from aria.planning.service import _content_hash
    title = "Add resize tests tomorrow"
    service = _FakeService().with_open_task_hash(_content_hash(title))
    extractor = _make_extractor_with_service(service)
    payload = {
        "tasks": [{"title": title, "confidence": 0.9}],
        "project_updates": [],
        "new_projects": [],
    }
    counts = await extractor._apply(payload, "conv1", ["m1"])
    assert counts["tasks_deduped"] == 1
    assert counts["tasks_proposed"] == 0
    assert service.created_tasks == []


@pytest.mark.asyncio
async def test_apply_attaches_to_existing_project_via_hint():
    project = Project(
        id="p1", name="ARIA", slug="aria", summary="", status="active",
        created_at=_now(), updated_at=_now(),
    )
    service = _FakeService().with_existing_project("ARIA", project)
    extractor = _make_extractor_with_service(service)
    payload = {
        "tasks": [
            {"title": "Ship v1", "confidence": 0.9, "project_hint": "ARIA"},
        ],
        "project_updates": [],
        "new_projects": [],
    }
    await extractor._apply(payload, "conv1", ["m1"])
    body, source = service.created_tasks[0]
    assert body.project_id == "p1"
    assert source.confidence == 0.9
    assert source.type == "conversation"


@pytest.mark.asyncio
async def test_apply_drops_project_update_with_no_match():
    service = _FakeService()  # no existing projects
    extractor = _make_extractor_with_service(service)
    payload = {
        "tasks": [],
        "project_updates": [
            {"project_hint": "Unknown", "status_note": "Made progress"}
        ],
        "new_projects": [],
    }
    counts = await extractor._apply(payload, "conv1", ["m1"])
    assert counts["projects_updated"] == 0
    assert service.activities == []


@pytest.mark.asyncio
async def test_apply_creates_new_projects_explicitly():
    service = _FakeService()
    extractor = _make_extractor_with_service(service)
    payload = {
        "tasks": [],
        "project_updates": [],
        "new_projects": [
            {"name": "Beacon", "summary": "Small Rust CLI"}
        ],
    }
    counts = await extractor._apply(payload, "conv1", ["m1"])
    assert counts["new_projects"] == 1
    assert service.created_projects[0].name == "Beacon"
    assert service.created_projects[0].summary == "Small Rust CLI"


@pytest.mark.asyncio
async def test_apply_appends_activity_and_updates_next_step():
    project = Project(
        id="p1", name="ARIA", slug="aria", summary="",
        next_steps=["existing step"], status="active",
        created_at=_now(), updated_at=_now(),
    )
    service = _FakeService().with_existing_project("ARIA", project)
    extractor = _make_extractor_with_service(service)
    payload = {
        "tasks": [],
        "project_updates": [
            {"project_hint": "ARIA", "status_note": "Shipped fix", "next_step": "Verify on device"}
        ],
        "new_projects": [],
    }
    await extractor._apply(payload, "conv1", ["m1"])
    assert service.activities == [("p1", "conversation:conv1", "Shipped fix")]
    # next_step is prepended
    pid, steps = service.next_steps_set[0]
    assert pid == "p1"
    assert steps[0] == "Verify on device"
