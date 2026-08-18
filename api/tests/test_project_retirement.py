"""
Tests for project retirement.

The property that matters most is ORDERING: memories are written and verified
before anything is deleted. A retirement that removed the project and then
failed to extract would destroy the only record of it, which is the exact
outcome the feature exists to prevent.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aria.planning.retirement import (
    MAX_TOTAL_CHARS,
    ProjectRetirementService,
    RetirementRefused,
)


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, n):
        return self._docs[:n]


class FakeColl:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.deleted = []

    def find(self, *_a, **_k):
        return FakeCursor(self.docs)

    async def find_one(self, *_a, **_k):
        return self.docs[0] if self.docs else None

    async def count_documents(self, *_a, **_k):
        return len(self.docs)

    async def delete_many(self, q):
        self.deleted.append(q)
        return SimpleNamespace(deleted_count=len(self.docs))


class FakeDB:
    def __init__(self, **colls):
        self._c = colls

    def __getattr__(self, name):
        return self._c.setdefault(name, FakeColl())


def make_project(**kw):
    return SimpleNamespace(
        id="pid-1",
        slug=kw.get("slug", "demo"),
        name=kw.get("name", "Demo"),
        summary="a demo project",
        path="/home/ben/Development/demo",
        relevant_paths=[],
        git={"branch": "master"},
        last_activity_at="2026-08-01",
        next_steps=["finish the thing"],
    )


def make_service(db, *, project=None, deleted_ok=True):
    planning = SimpleNamespace(
        get_project_by_slug=AsyncMock(return_value=project),
        get_project=AsyncMock(return_value=project),
        delete_project=AsyncMock(return_value=deleted_ok),
    )
    svc = ProjectRetirementService(db, planning)
    return svc, planning


@pytest.mark.asyncio
async def test_refuses_while_work_is_live():
    """Retiring under a running agent would strand it."""
    project = make_project()
    db = FakeDB(
        projects=FakeColl([{"path": "/home/ben/Development/demo"}]),
        shells=FakeColl([{"name": "claude-demo", "project_dir": "/home/ben/Development/demo", "status": "active"}]),
        coding_sessions=FakeColl([]),
        tasks=FakeColl([]),
    )
    svc, planning = make_service(db, project=project)

    with pytest.raises(RetirementRefused, match="active shell"):
        await svc.retire("demo")
    planning.delete_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_touches_nothing():
    project = make_project()
    db = FakeDB(
        projects=FakeColl([{"path": "/home/ben/Development/demo"}]),
        shells=FakeColl([]),
        coding_sessions=FakeColl([]),
        tasks=FakeColl([]),
    )
    svc, planning = make_service(db, project=project)

    with patch.object(svc.memory, "create_memory", AsyncMock()) as create:
        report = await svc.retire("demo", dry_run=True)

    assert report["dry_run"] is True
    assert report["deleted"] is False
    create.assert_not_awaited()
    planning.delete_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_is_kept_when_no_memory_could_be_written():
    """The whole point: never delete the row if the record did not survive."""
    project = make_project()
    db = FakeDB(
        projects=FakeColl([{"path": "/home/ben/Development/demo"}]),
        shells=FakeColl([]),
        coding_sessions=FakeColl([]),
        tasks=FakeColl([]),
    )
    svc, planning = make_service(db, project=project)

    with patch.object(svc.memory, "create_memory", AsyncMock(return_value=None)), \
         patch.object(svc.extractor, "extract_from_text", AsyncMock(return_value=[])):
        with pytest.raises(RetirementRefused, match="Nothing was deleted"):
            await svc.retire("demo")

    planning.delete_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_extraction_failure_still_retires_with_the_record():
    """A dead extraction LLM must not block retirement — the deterministic
    record is written first precisely so this case still preserves something."""
    project = make_project()
    db = FakeDB(
        projects=FakeColl([{"path": "/home/ben/Development/demo"}]),
        shells=FakeColl([]),
        coding_sessions=FakeColl([]),
        tasks=FakeColl([]),
        memories=FakeColl([{"_id": "m1"}]),
    )
    svc, planning = make_service(db, project=project)

    with patch.object(svc.memory, "create_memory", AsyncMock(return_value="m1")), \
         patch.object(svc.extractor, "extract_from_text", AsyncMock(side_effect=RuntimeError("llm down"))):
        report = await svc.retire("demo")

    assert report["memories_written"] == ["m1"]
    assert report["deleted"] is True
    planning.delete_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_names_what_was_kept_and_what_went():
    project = make_project()
    db = FakeDB(
        projects=FakeColl([{"path": "/home/ben/Development/demo"}]),
        shells=FakeColl([]),
        coding_sessions=FakeColl([]),
        tasks=FakeColl([]),
    )
    svc, _ = make_service(db, project=project)
    gathered = await svc.gather(project)
    record = svc._record_memory(project, gathered)

    assert "Demo" in record and "demo" in record
    assert "finish the thing" in record          # unfinished work is part of the record
    assert "Scrollback and session records were kept" in record


@pytest.mark.asyncio
async def test_attribution_does_not_steal_a_child_projects_shells():
    """Most-specific-root wins. A coarse parent (~/Development) must not claim
    a child project's transcripts — the same rule PathIndex enforces."""
    parent = make_project(slug="development", name="Development")
    parent.path = "/home/ben/Development"
    db = FakeDB(
        projects=FakeColl([
            {"path": "/home/ben/Development"},
            {"path": "/home/ben/Development/demo"},
        ]),
        shells=FakeColl([
            {"name": "claude-demo", "project_dir": "/home/ben/Development/demo", "status": "stopped"},
            {"name": "claude-dev", "project_dir": "/home/ben/Development", "status": "stopped"},
        ]),
        coding_sessions=FakeColl([]),
        tasks=FakeColl([]),
    )
    svc, _ = make_service(db, project=parent)

    gathered = await svc.gather(parent)
    names = [s["name"] for s in gathered["shells"]]
    assert names == ["claude-dev"], f"parent swallowed a child's shells: {names}"


@pytest.mark.asyncio
async def test_transcript_is_bounded():
    """`shell_events` holds millions of rows; retirement must read a slice."""
    project = make_project()
    huge = [{"content": "x" * 500, "line_number": i} for i in range(5000)]
    db = FakeDB(
        projects=FakeColl([{"path": "/home/ben/Development/demo"}]),
        shells=FakeColl([]),
        coding_sessions=FakeColl([]),
        tasks=FakeColl([]),
        shell_events=FakeColl(huge),
    )
    svc, _ = make_service(db, project=project)

    text, chars = await svc._transcript_text([{"name": "claude-demo"}])
    assert chars <= MAX_TOTAL_CHARS
    assert len(text) <= MAX_TOTAL_CHARS + 200  # plus the header line
