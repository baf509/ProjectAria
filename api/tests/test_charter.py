"""
ARIA - Project charters + active-set hygiene tests

Purpose: lock the two rules the steward layer stands on (proposal §4).

  1. A charter is HUMAN-owned. A worker may propose (db.scan_review) but must
     never write one — an agent that can edit its own charter can edit its own
     autonomy level, budget and allowed paths.
  2. Discovery is not curation. The harvester registered 59 rows on 2026-08-15,
     all status=active, including ~/Downloads, /tmp/workspace, ~/venv, the
     Obsidian vault, three pi smoke dirs and four .worktrees checkouts. Those
     become kind=ignored (reconciled, not deleted), and the human-editable
     fields on every row — summary, status, tags, check_command, charter,
     relevant_paths — survive a re-harvest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId
from httpx import ASGITransport, AsyncClient

from aria.config import settings
from aria.planning.models import Charter, ProjectUpdateRequest
from aria.planning.service import PlanningService, effective_budget
from aria.shells import harvest as harvest_mod
from aria.shells.harvest import harvest


# --------------------------------------------------------------- fake mongo

def _dotted_set(doc: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _dotted_get(doc: dict, path: str):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _match_value(doc_value, cond) -> bool:
    if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            if op == "$ne" and doc_value == arg:
                return False
            if op == "$in" and doc_value not in arg:
                return False
            if op == "$nin" and doc_value in arg:
                return False
            if op == "$exists" and (doc_value is not None) != bool(arg):
                return False
        return True
    if isinstance(doc_value, list):
        return cond in doc_value
    return doc_value == cond


def _matches(doc: dict, query: dict) -> bool:
    for field, cond in (query or {}).items():
        if field == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
            continue
        if not _match_value(_dotted_get(doc, field), cond):
            return False
    return True


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    async def to_list(self, length=None):
        return list(self._docs)

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d

        return gen()


class _FakeCollection:
    """Enough of motor's contract to exercise $set / $setOnInsert / $max and
    dotted paths — the four update shapes this feature actually uses."""

    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]
        self.updates: list[dict] = []

    async def find_one(self, query=None, projection=None):
        for doc in self.docs:
            if _matches(doc, query or {}):
                return dict(doc)
        return None

    def find(self, query=None, projection=None):
        return _FakeCursor([dict(d) for d in self.docs if _matches(d, query or {})])

    async def insert_one(self, doc):
        doc.setdefault("_id", f"id{len(self.docs)}")
        self.docs.append(dict(doc))
        return MagicMock(inserted_id=doc["_id"])

    async def update_one(self, key, update, upsert=False):
        self.updates.append({"key": key, "update": update, "upsert": upsert})
        target = next((d for d in self.docs if _matches(d, key)), None)
        if target is None:
            if not upsert:
                return MagicMock(modified_count=0, matched_count=0)
            target = {k: v for k, v in key.items() if not isinstance(v, dict)}
            target.setdefault("_id", f"id{len(self.docs)}")
            for field, value in (update.get("$setOnInsert") or {}).items():
                _dotted_set(target, field, value)
            self.docs.append(target)
        for field, value in (update.get("$set") or {}).items():
            _dotted_set(target, field, value)
        for field, value in (update.get("$max") or {}).items():
            current = _dotted_get(target, field)
            if current is None or (value is not None and value > current):
                _dotted_set(target, field, value)
        return MagicMock(modified_count=1, matched_count=1)

    async def update_many(self, *a, **kw):
        return MagicMock(modified_count=0)

    async def delete_one(self, *a, **kw):
        return MagicMock(deleted_count=0)


class _FakeShells:
    def find(self, *a, **kw):
        return _FakeCursor([])


class _FakeDB:
    def __init__(self, projects=None):
        self.projects = _FakeCollection(projects)
        self.tasks = _FakeCollection()
        self.shells = _FakeShells()
        self.scan_review = _FakeCollection()

    def __getitem__(self, name):
        return getattr(self, name)


def _now():
    return datetime.now(timezone.utc)


def _project_doc(slug="aria", **over) -> dict:
    doc = {
        "_id": f"id-{slug}",
        "slug": slug,
        "name": slug,
        "summary": "",
        "status": "active",
        "kind": "project",
        "next_steps": [],
        "relevant_paths": [],
        "tags": [],
        "recent_activity": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    doc.update(over)
    return doc


# ------------------------------------------------------------ effective_budget

def test_effective_budget_falls_back_to_settings():
    """Unset budget fields resolve from config, not from the schema — retuning
    the fleet must not require rewriting every charter."""
    resolved = effective_budget(None)
    assert resolved["sessions_per_day"] == settings.steward_default_sessions_per_day
    assert resolved["session_minutes"] == settings.steward_default_session_minutes
    assert resolved["cloud_usd_per_day"] == settings.steward_default_cloud_usd_per_day
    assert resolved["research_runs_per_week"] == settings.steward_default_research_runs_per_week
    assert resolved["lines_merge"] == settings.steward_default_lines_merge
    # No configured default: local tokens are gated by slot scheduling, not a count.
    assert resolved["local_tokens_per_day"] is None


def test_effective_budget_charter_wins_and_zero_is_not_unset():
    charter = Charter(budget={"sessions_per_day": 0, "cloud_usd_per_day": 0.5})
    resolved = effective_budget(charter)
    assert resolved["sessions_per_day"] == 0, "0 sessions/day is a real cap, not 'unset'"
    assert resolved["cloud_usd_per_day"] == 0.5
    assert resolved["lines_merge"] == settings.steward_default_lines_merge


def test_effective_budget_accepts_a_raw_doc():
    assert effective_budget({"budget": {"session_minutes": 15}})["session_minutes"] == 15


# ------------------------------------------------------------------- charters

@pytest.mark.asyncio
async def test_charter_round_trip_stamps_approval_and_promotes_kind():
    db = _FakeDB([_project_doc("war-audio-game", kind="scratch")])
    svc = PlanningService(db)

    proj = await svc.set_charter(
        "war-audio-game",
        {"purpose": "Ship the audio engine", "goals": ["mix bus"], "autonomy": 2},
        actor="human",
        via="vault",
    )
    assert proj is not None
    assert proj.charter.purpose == "Ship the audio engine"
    assert proj.charter.autonomy == 2
    assert proj.charter.approved_via == "vault"
    assert proj.charter.approved_at is not None
    # A human writing a purpose IS the statement that this is a project; without
    # the promotion the charter would sit outside the active set forever.
    assert proj.kind == "project"


@pytest.mark.asyncio
async def test_charter_partial_merge_keeps_untouched_fields():
    db = _FakeDB([_project_doc("aria")])
    svc = PlanningService(db)
    await svc.set_charter(
        "aria",
        {"purpose": "Be the steward", "goals": ["A", "B"], "budget": {"sessions_per_day": 5}},
        actor="human",
        via="api",
    )
    # A vault edit that only touches one budget key must not blank the others,
    # and must not blank purpose/goals either.
    proj = await svc.set_charter("aria", {"budget": {"lines_merge": 50}}, actor="human", via="vault")
    assert proj.charter.purpose == "Be the steward"
    assert proj.charter.goals == ["A", "B"]
    assert proj.charter.budget.sessions_per_day == 5
    assert proj.charter.budget.lines_merge == 50
    assert effective_budget(proj.charter)["sessions_per_day"] == 5


@pytest.mark.asyncio
async def test_worker_cannot_overwrite_a_human_charter():
    db = _FakeDB([_project_doc("aria")])
    svc = PlanningService(db)
    await svc.set_charter("aria", {"purpose": "Ben's purpose", "autonomy": 1}, actor="human", via="api")

    proj = await svc.set_charter(
        "aria",
        {"purpose": "my own purpose", "autonomy": 3},
        actor="steward-worker",
        via="api",
    )

    assert proj.charter.purpose == "Ben's purpose", "a worker overwrote a human charter"
    assert proj.charter.autonomy == 1, "a worker escalated its own autonomy level"
    review = db.scan_review.docs
    assert len(review) == 1 and review[0]["kind"] == "charter_proposal"
    assert review[0]["subject"] == "aria"


@pytest.mark.asyncio
async def test_worker_proposal_is_not_silently_dropped_when_no_charter_exists():
    """The empty-charter case is the one merge_owned would let through as a
    no-op: no existing value means no contradiction. It still must not write."""
    db = _FakeDB([_project_doc("aria")])
    svc = PlanningService(db)

    proj = await svc.set_charter("aria", {"purpose": "invented"}, actor="steward-worker", via="api")

    assert proj.charter is None
    assert [d["kind"] for d in db.scan_review.docs] == ["charter_proposal"]


@pytest.mark.asyncio
async def test_charter_records_per_field_provenance():
    db = _FakeDB([_project_doc("aria")])
    svc = PlanningService(db)
    await svc.set_charter("aria", {"purpose": "p"}, actor="human", via="mcp")
    stored = await db.projects.find_one({"slug": "aria"})
    assert stored["charter"]["source"]["purpose"]["actor"] == "human"
    assert stored["source"]["charter"]["via"] == "mcp"


@pytest.mark.asyncio
async def test_set_charter_rejects_an_unknown_approval_surface():
    db = _FakeDB([_project_doc("aria")])
    svc = PlanningService(db)
    with pytest.raises(ValueError):
        await svc.set_charter("aria", {"purpose": "p"}, actor="human", via="telepathy")


@pytest.mark.asyncio
async def test_patch_project_merges_charter_instead_of_replacing_it():
    oid = ObjectId()  # update_project addresses by ObjectId, unlike set_charter
    db = _FakeDB([_project_doc("aria", _id=oid)])
    svc = PlanningService(db)
    await svc.set_charter("aria", {"purpose": "keep me", "goals": ["g"]}, actor="human", via="api")

    proj = await svc.update_project(
        str(oid), ProjectUpdateRequest(charter=Charter(**{"goals": ["g", "h"]}))
    )

    assert proj.charter.purpose == "keep me", "PATCH replaced the charter instead of merging"
    assert proj.charter.goals == ["g", "h"]


# ----------------------------------------------------------------- active set

@pytest.mark.asyncio
async def test_active_projects_is_status_kind_and_purpose():
    chartered = {"purpose": "ship it", "approved_at": _now(), "approved_via": "api"}
    db = _FakeDB([
        _project_doc("keeper", charter=dict(chartered)),
        _project_doc("paused-one", status="paused", charter=dict(chartered)),
        _project_doc("scratch-one", kind="scratch", charter=dict(chartered)),
        _project_doc("ignored-one", kind="ignored", charter=dict(chartered)),
        _project_doc("no-charter"),
        _project_doc("blank-purpose", charter={"purpose": "   "}),
    ])
    svc = PlanningService(db)

    slugs = [p.slug for p in await svc.active_projects()]

    assert slugs == ["keeper"]


@pytest.mark.asyncio
async def test_active_projects_includes_rows_predating_the_kind_field():
    """All 59 live rows have no `kind` at all; a chartered one must not vanish
    from the steward's world just because the field was added later."""
    doc = _project_doc("legacy", charter={"purpose": "still a project"})
    doc.pop("kind")
    svc = PlanningService(_FakeDB([doc]))
    assert [p.slug for p in await svc.active_projects()] == ["legacy"]


@pytest.mark.asyncio
async def test_active_projects_survives_an_unparseable_row():
    """One malformed document must not blind the steward to every project."""
    bad = _project_doc("broken", status="nonsense-status", charter={"purpose": "x"})
    good = _project_doc("fine", charter={"purpose": "x"})
    svc = PlanningService(_FakeDB([bad, good]))
    assert [p.slug for p in await svc.active_projects()] == ["fine"]


@pytest.mark.asyncio
async def test_propose_pause_never_touches_status():
    db = _FakeDB([_project_doc("aria")])
    svc = PlanningService(db)

    assert await svc.propose_pause("aria", "21 days idle") is True

    stored = await db.projects.find_one({"slug": "aria"})
    assert stored["status"] == "active", "the steward changed a human-owned lifecycle field"
    assert stored["steward"]["paused_reason"] == "21 days idle"
    assert db.scan_review.docs[0]["kind"] == "project_pause_proposal"


@pytest.mark.asyncio
async def test_update_steward_state_rejects_foreign_fields():
    db = _FakeDB([_project_doc("aria")])
    svc = PlanningService(db)
    await svc.update_steward_state("aria", {"no_progress_streak": 2, "status": "archived"})
    stored = await db.projects.find_one({"slug": "aria"})
    assert stored["steward"]["no_progress_streak"] == 2
    assert stored["status"] == "active"


# ------------------------------------------------------------------- harvester

JUNK_PATHS = [
    "/home/ben/Downloads",
    "/home/ben/Desktop",
    "/home/ben/Documents",
    "/home/ben/Public",
    "/home/ben/venv",
    "/home/ben/Obsidian/vault",
    "/home/ben/Development",           # a harvest root; swallows its children
    "/tmp",
    "/tmp/workspace",
    "/tmp/aria-pi-smoke.TTVu8p",
    "/tmp/aria-pi-session-id-smoke.lltD52",
    "/home/ben/Development/war-audio-game/.worktrees/ridge_review-20260731-022930",
    "/home/ben/Development/infrastructure/rocmfpx-decode-fusion-wt",
    "/home/ben/x/node_modules/pkg",
]


@pytest.mark.parametrize("path", JUNK_PATHS)
def test_harvest_ignore_covers_every_junk_row_seen_live(path):
    assert harvest_mod._is_ignored(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/home/ben/Development/ProjectAria",
        "/home/ben/Development/infrastructure",
        "/home/ben/Development/Hermes",
        "/home/ben/Development/Games/theVeilWar",
    ],
)
def test_harvest_ignore_keeps_real_projects(path):
    assert harvest_mod._is_ignored(path) is False


def test_looks_like_project_needs_evidence(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert harvest_mod._looks_like_project(str(bare)) is False
    (bare / "CLAUDE.md").write_text("# explained to somebody\n")
    assert harvest_mod._looks_like_project(str(bare)) is True


def _patch_discovery(monkeypatch, path: str):
    monkeypatch.setattr(harvest_mod, "_find_git_repos", lambda roots, max_depth=3: [path])
    monkeypatch.setattr(harvest_mod, "_gather_claude", lambda: {})
    monkeypatch.setattr(harvest_mod, "_gather_pi", lambda: {})
    monkeypatch.setattr(harvest_mod, "_canonical", lambda p: path)


@pytest.mark.asyncio
async def test_harvest_marks_junk_paths_ignored_rather_than_skipping(monkeypatch):
    """An ignored path is still upserted: the 20 junk rows already in the
    collection only get fixed if the harvester reconciles them."""
    db = _FakeDB()
    _patch_discovery(monkeypatch, "/tmp/workspace")

    result = await harvest(db, roots=["/tmp"])

    assert result["ignored"] == 1
    stored = await db.projects.find_one({"slug": "workspace"})
    assert stored["kind"] == "ignored"
    assert stored["source"]["kind"]["actor"] == harvest_mod.HARVEST_ACTOR


@pytest.mark.asyncio
async def test_harvest_reconciles_a_live_shaped_junk_row(monkeypatch):
    """The shape of all 59 live rows: no `kind` field at all. Backfilling them
    is the whole point of reconciling instead of skipping ignored paths."""
    doc = _project_doc("workspace", path="/tmp/workspace")
    doc.pop("kind")
    db = _FakeDB([doc])
    _patch_discovery(monkeypatch, "/tmp/workspace")

    await harvest(db, roots=["/tmp"])

    assert (await db.projects.find_one({"slug": "workspace"}))["kind"] == "ignored"


@pytest.mark.asyncio
async def test_harvest_may_reclassify_its_own_earlier_guess(monkeypatch):
    db = _FakeDB([
        _project_doc(
            "workspace",
            path="/tmp/workspace",
            kind="project",
            source={"kind": {"actor": harvest_mod.HARVEST_ACTOR}},
        )
    ])
    _patch_discovery(monkeypatch, "/tmp/workspace")

    await harvest(db, roots=["/tmp"])

    stored = await db.projects.find_one({"slug": "workspace"})
    assert stored["kind"] == "ignored", "a harvester-set kind must be reconcilable"


@pytest.mark.asyncio
async def test_harvest_never_overwrites_a_human_kind_and_proposes_instead(monkeypatch):
    db = _FakeDB([
        _project_doc(
            "workspace",
            path="/tmp/workspace",
            kind="project",
            source={"kind": {"actor": "human"}},
        )
    ])
    _patch_discovery(monkeypatch, "/tmp/workspace")

    result = await harvest(db, roots=["/tmp"])

    stored = await db.projects.find_one({"slug": "workspace"})
    assert stored["kind"] == "project", "a human decision was overwritten"
    assert result["kind_conflicts"] == 1
    assert db.scan_review.docs[0]["kind"] == "project_kind_conflict"


@pytest.mark.asyncio
async def test_harvest_classifies_new_repos_by_evidence(monkeypatch):
    for looks_real, expected in ((True, "project"), (False, "scratch")):
        db = _FakeDB()
        _patch_discovery(monkeypatch, "/home/ben/Development/fresh-thing")
        monkeypatch.setattr(harvest_mod, "_looks_like_project", lambda p: looks_real)

        await harvest(db, roots=["/home/ben/Development"])

        stored = await db.projects.find_one({"slug": "fresh-thing"})
        assert stored["kind"] == expected


@pytest.mark.asyncio
async def test_harvest_merges_relevant_paths_keeping_human_entries(monkeypatch):
    """relevant_paths is human-editable via ProjectUpdateRequest but was being
    flattened to the discovered set every 30 minutes."""
    db = _FakeDB([
        _project_doc(
            "fresh-thing",
            path="/home/ben/Development/fresh-thing",
            relevant_paths=["/home/ben/Development/fresh-thing", "/home/ben/notes/by-hand"],
        )
    ])
    _patch_discovery(monkeypatch, "/home/ben/Development/fresh-thing")
    monkeypatch.setattr(harvest_mod, "_looks_like_project", lambda p: True)

    await harvest(db, roots=["/home/ben/Development"])

    stored = await db.projects.find_one({"slug": "fresh-thing"})
    assert "/home/ben/notes/by-hand" in stored["relevant_paths"]
    assert "/home/ben/Development/fresh-thing" in stored["relevant_paths"]


@pytest.mark.asyncio
async def test_harvest_leaves_every_human_field_alone_on_reharvest(monkeypatch):
    db = _FakeDB([
        _project_doc(
            "fresh-thing",
            path="/home/ben/Development/fresh-thing",
            summary="hand written",
            status="paused",
            tags=["keep"],
            check_command="make check",
            charter={"purpose": "hand written purpose"},
        )
    ])
    _patch_discovery(monkeypatch, "/home/ben/Development/fresh-thing")
    monkeypatch.setattr(harvest_mod, "_looks_like_project", lambda p: True)

    await harvest(db, roots=["/home/ben/Development"])

    written = db.projects.updates[-1]["update"]["$set"]
    for human_field in ("summary", "status", "tags", "check_command", "charter", "name", "next_steps"):
        assert human_field not in written, f"harvester wrote human-owned {human_field}"
    stored = await db.projects.find_one({"slug": "fresh-thing"})
    assert stored["status"] == "paused"
    assert stored["charter"]["purpose"] == "hand written purpose"


@pytest.mark.asyncio
async def test_harvest_sets_last_signal_at_from_real_activity(monkeypatch):
    """last_signal_at was null on all 59 rows, so staleness detection had only
    updated_at — which this worker bumps every 30 minutes regardless."""
    seen = _now() - timedelta(hours=3)
    db = _FakeDB()
    _patch_discovery(monkeypatch, "/home/ben/Development/fresh-thing")
    monkeypatch.setattr(harvest_mod, "_looks_like_project", lambda p: True)
    monkeypatch.setattr(
        harvest_mod, "_gather_claude",
        lambda: {"/home/ben/Development/fresh-thing": {"last_activity": seen, "sessions": 2}},
    )

    await harvest(db, roots=["/home/ben/Development"])
    stored = await db.projects.find_one({"slug": "fresh-thing"})
    assert stored["last_signal_at"] == seen

    # A newer in-band signal (append_project_activity) must not be walked back
    # by the next harvest tick: $max, never $set.
    fresher = _now()
    stored_doc = db.projects.docs[0]
    stored_doc["last_signal_at"] = fresher
    await harvest(db, roots=["/home/ben/Development"])
    assert db.projects.docs[0]["last_signal_at"] == fresher


@pytest.mark.asyncio
async def test_harvest_writes_nothing_human_on_insert_beyond_defaults(monkeypatch):
    db = _FakeDB()
    _patch_discovery(monkeypatch, "/home/ben/Development/fresh-thing")
    monkeypatch.setattr(harvest_mod, "_looks_like_project", lambda p: True)

    await harvest(db, roots=["/home/ben/Development"])

    on_insert = db.projects.updates[-1]["update"]["$setOnInsert"]
    assert on_insert["charter"] is None and on_insert["steward"] is None
    assert on_insert["status"] == "active" and on_insert["summary"] == ""


# --------------------------------------------------------------------- routes

@pytest.fixture
async def client():
    from aria.main import app
    from aria.api import deps

    db = _FakeDB([
        _project_doc("aria", kind="scratch"),
        _project_doc("noise", kind="ignored"),
    ])
    service = PlanningService(db)
    app.dependency_overrides[deps.get_planning_service] = lambda: service
    app.dependency_overrides[deps.get_db] = lambda: db

    rl = MagicMock()
    rl.check = MagicMock(return_value=(True, 100))
    with (
        patch("aria.main.settings") as mock_settings,
        patch("aria.main.get_rate_limiter", return_value=rl),
    ):
        mock_settings.api_auth_enabled = False
        mock_settings.cors_origins = ["*"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            ac.db = db  # type: ignore[attr-defined]
            yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_charter_route_round_trip(client):
    resp = await client.put(
        "/api/v1/projects/aria/charter",
        json={"charter": {"purpose": "Be the steward", "autonomy": 2}, "via": "vault"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["charter"]["purpose"] == "Be the steward"
    assert body["charter"]["approved_via"] == "vault"
    assert body["kind"] == "project"
    assert body["effective_budget"]["sessions_per_day"] == settings.steward_default_sessions_per_day

    got = await client.get("/api/v1/projects/aria/charter")
    assert got.status_code == 200
    assert got.json()["charter"]["autonomy"] == 2


@pytest.mark.asyncio
async def test_charter_route_partial_patch_does_not_blank(client):
    await client.put(
        "/api/v1/projects/aria/charter",
        json={"charter": {"purpose": "P", "research_topics": ["t1"]}},
    )
    resp = await client.put(
        "/api/v1/projects/aria/charter", json={"charter": {"goals": ["g1"]}}
    )
    body = resp.json()["charter"]
    assert body["purpose"] == "P"
    assert body["research_topics"] == ["t1"]
    assert body["goals"] == ["g1"]


@pytest.mark.asyncio
async def test_charter_route_404(client):
    resp = await client.get("/api/v1/projects/nope/charter")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_charter_route_rejects_out_of_range_autonomy(client):
    resp = await client.put(
        "/api/v1/projects/aria/charter", json={"charter": {"autonomy": 7}}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_active_set_route_is_not_shadowed(client):
    """`/projects/active-set` must resolve to the active set, never to
    `/projects/{project_id}` (which would 404) and never to digest's
    `/projects/active` pointer."""
    await client.put(
        "/api/v1/projects/aria/charter", json={"charter": {"purpose": "Be the steward"}}
    )
    resp = await client.get("/api/v1/projects/active-set")
    assert resp.status_code == 200, resp.text
    assert [p["slug"] for p in resp.json()["projects"]] == ["aria"]
