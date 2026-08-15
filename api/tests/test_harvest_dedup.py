"""
ARIA - Project harvester deduplication tests

Purpose: a harvested project must never shadow a hand-created one that already
claims the same path.

The bug this locks: the harvester upserts on `{"slug": <directory name>}`, so
the hand-created project "ARIA" (slug `aria`, real summary, claiming
~/Development/ProjectAria via `relevant_paths`) got a harvested twin
"ProjectAria" (slug from the directory) with an empty summary. Both rows then
claimed the same root, which:
  - split that project's memories, cockpit rollups and path attribution in two;
  - made path->project attribution depend on Mongo's iteration order;
  - left the row the UI showed with no summary.

Found 2026-08-07 while building the ontology projection, which surfaced it as a
scan_review conflict.
"""

import pytest

from aria.shells.harvest import harvest


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d

        return gen()


class _FakeProjects:
    """Minimal stand-in recording how each upsert was keyed."""

    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.calls = []

    async def find_one(self, query, projection=None):
        # Only the path-claim lookup is exercised here.
        or_clauses = query.get("$or") or []
        ne_slug = (query.get("slug") or {}).get("$ne")
        for doc in self.docs:
            if ne_slug is not None and doc.get("slug") == ne_slug:
                continue
            for clause in or_clauses:
                if "path" in clause and doc.get("path") == clause["path"]:
                    return doc
                if "relevant_paths" in clause and clause["relevant_paths"] in (
                    doc.get("relevant_paths") or []
                ):
                    return doc
        return None

    async def update_one(self, key, update, upsert=False):
        self.calls.append({"key": key, "update": update, "upsert": upsert})


class _FakeShells:
    def find(self, *a, **kw):
        return _FakeCursor([])


class _FakeDB:
    def __init__(self, projects):
        self.projects = projects
        self.shells = _FakeShells()


def _curated_aria(path: str) -> dict:
    """The hand-created row: owns the prose, claims the path via
    `relevant_paths`, and has NO `path` field at all — exactly the shape that
    slipped past a path-only lookup in production."""
    return {
        "_id": "curated-id",
        "slug": "aria",
        "name": "ARIA",
        "summary": "Local-first personal AI agent",
        "relevant_paths": [path],
    }


async def test_harvest_matches_existing_project_by_relevant_paths(
    tmp_path, monkeypatch
):
    """A directory already claimed by another project must refresh THAT row,
    not create a second one keyed on the directory name."""
    repo = tmp_path / "ProjectAria"
    repo.mkdir()

    projects = _FakeProjects([_curated_aria(str(repo))])
    db = _FakeDB(projects)

    monkeypatch.setattr(
        "aria.shells.harvest._find_git_repos", lambda roots, max_depth=3: [str(repo)]
    )
    monkeypatch.setattr("aria.shells.harvest._gather_claude", lambda: {})
    monkeypatch.setattr("aria.shells.harvest._gather_pi", lambda: {})
    monkeypatch.setattr(
        "aria.shells.harvest._canonical", lambda p: str(repo)
    )

    result = await harvest(db, roots=[str(tmp_path)])

    assert result["matched_existing"] == 1, "should have matched the curated row"
    assert len(projects.calls) == 1
    key = projects.calls[0]["key"]
    assert key == {"_id": "curated-id"}, (
        f"expected an _id-keyed update of the existing project, got {key} — "
        "a slug-keyed upsert is what created the duplicate"
    )


async def test_harvest_preserves_curated_summary(tmp_path, monkeypatch):
    """The harvester may write structure, never prose. `summary` and `name`
    live in $setOnInsert, so refreshing an existing row must not touch them."""
    repo = tmp_path / "ProjectAria"
    repo.mkdir()
    projects = _FakeProjects([_curated_aria(str(repo))])
    db = _FakeDB(projects)

    monkeypatch.setattr(
        "aria.shells.harvest._find_git_repos", lambda roots, max_depth=3: [str(repo)]
    )
    monkeypatch.setattr("aria.shells.harvest._gather_claude", lambda: {})
    monkeypatch.setattr("aria.shells.harvest._gather_pi", lambda: {})
    monkeypatch.setattr("aria.shells.harvest._canonical", lambda p: str(repo))

    await harvest(db, roots=[str(tmp_path)])

    update = projects.calls[0]["update"]
    assert "summary" not in update["$set"]
    assert "name" not in update["$set"]
    # Structure IS the harvester's to write.
    assert update["$set"]["path"] == str(repo)


async def test_harvest_still_creates_genuinely_new_projects(tmp_path, monkeypatch):
    """The path check must not block real discovery — an unclaimed directory
    still registers under its own slug."""
    repo = tmp_path / "brand-new-thing"
    repo.mkdir()
    projects = _FakeProjects([])  # nothing claims it
    db = _FakeDB(projects)

    monkeypatch.setattr(
        "aria.shells.harvest._find_git_repos", lambda roots, max_depth=3: [str(repo)]
    )
    monkeypatch.setattr("aria.shells.harvest._gather_claude", lambda: {})
    monkeypatch.setattr("aria.shells.harvest._gather_pi", lambda: {})
    monkeypatch.setattr("aria.shells.harvest._canonical", lambda p: str(repo))

    result = await harvest(db, roots=[str(tmp_path)])

    assert result["matched_existing"] == 0
    assert projects.calls[0]["key"] == {"slug": "brand-new-thing"}
    assert projects.calls[0]["upsert"] is True
