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
from pymongo.errors import WriteError

from aria.config import settings
from aria.planning.models import (
    Charter,
    ProjectCreateRequest,
    ProjectUpdateRequest,
)
from aria.planning import service as planning_service
from aria.planning.service import (
    CharterRefused,
    PlanningService,
    active_set_blockers,
    effective_budget,
)
from aria.shells import harvest as harvest_mod
from aria.shells.harvest import harvest


# --------------------------------------------------------------- fake mongo

def _dotted_set(doc: dict, path: str, value) -> None:
    """A dotted `$set`, INCLUDING MongoDB's refusal to address into a non-document
    parent. Verified against the live mongod 8.2.0:

        {"$set": {"steward.paused_reason": ...}} on {steward: null}
        -> WriteError: Cannot create field 'paused_reason' in element {steward: null}

    A MISSING parent auto-creates; a NULL one does not. The fake used to create
    the parent either way, which is exactly why a green fake-only suite coexisted
    with a `steward: None` on every newly created project that made the first
    steward tick raise. Do not "simplify" this back."""
    parts = path.split(".")
    cur = doc
    for depth, part in enumerate(parts[:-1]):
        nxt = cur.get(part)
        if part in cur and not isinstance(nxt, dict):
            raise WriteError(
                f"Cannot create field '{parts[depth + 1]}' in element "
                f"{{{part}: {nxt!r}}}"
            )
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


def test_harvest_actor_constant_agrees_with_the_harvester():
    """set_charter tells a glob-set `ignored` from a human-set one by this
    actor string; if the two copies drift, every ignored row starts reading as
    a human decision and charters get refused wholesale."""
    assert planning_service.HARVEST_ACTOR == harvest_mod.HARVEST_ACTOR


@pytest.mark.asyncio
async def test_charter_promotes_a_row_the_globs_marked_ignored():
    """kind=ignored is a GLOB verdict on all 18 live ignored rows
    (source.kind.actor=project-harvester) — HARVEST_IGNORE_NAMES matches
    basenames like `*-wt` and `session-*` anywhere on the box. A human charter
    outranks a glob; the alternative was a 200 that did nothing."""
    db = _FakeDB([
        _project_doc(
            "rocmfpx-decode-fusion-wt",
            kind="ignored",
            source={"kind": {"actor": harvest_mod.HARVEST_ACTOR}},
        )
    ])
    svc = PlanningService(db)

    proj = await svc.set_charter(
        "rocmfpx-decode-fusion-wt", {"purpose": "Land the decode fusion"},
        actor="human", via="vault",
    )

    assert proj.kind == "project"
    assert [p.slug for p in await svc.active_projects()] == ["rocmfpx-decode-fusion-wt"]


@pytest.mark.asyncio
async def test_charter_on_a_human_ignored_row_is_refused_not_swallowed():
    """The one case that is a real human 'no'. It must fail loudly and leave a
    review row — never store a charter nothing will ever read."""
    db = _FakeDB([
        _project_doc("noise", kind="ignored", source={"kind": {"actor": "human"}})
    ])
    svc = PlanningService(db)

    with pytest.raises(CharterRefused) as excinfo:
        await svc.set_charter("noise", {"purpose": "sneak it in"}, actor="human", via="api")

    assert "kind=ignored" in excinfo.value.reason
    assert "kind=project" in excinfo.value.remedy
    stored = await db.projects.find_one({"slug": "noise"})
    assert stored.get("charter") is None, "a refused charter was stored anyway"
    assert stored["kind"] == "ignored"
    assert [d["kind"] for d in db.scan_review.docs] == ["charter_kind_conflict"]


@pytest.mark.asyncio
async def test_explicit_kind_on_create_is_recorded_as_a_human_decision():
    """What makes the refusal above possible: a hand-created `ignored` row has
    to be distinguishable from a glob-classified one."""
    db = _FakeDB()
    svc = PlanningService(db)
    await svc.create_project(ProjectCreateRequest(name="Noise", kind="ignored"))
    stored = await db.projects.find_one({"slug": "noise"})
    assert stored["source"]["kind"]["actor"] == "human"

    with pytest.raises(CharterRefused):
        await svc.set_charter("noise", {"purpose": "p"}, actor="human", via="api")

    # ...and a project created without naming a kind stays the harvester's to
    # reclassify (the ambient extractor never passes one).
    await svc.create_project(ProjectCreateRequest(name="Ambient"))
    assert "source" not in (await db.projects.find_one({"slug": "ambient"}))


@pytest.mark.asyncio
async def test_charter_without_a_purpose_never_promotes_or_refuses():
    """A budget-only amendment is not a claim that this is a project."""
    db = _FakeDB([
        _project_doc("noise", kind="ignored", source={"kind": {"actor": "human"}})
    ])
    svc = PlanningService(db)
    proj = await svc.set_charter("noise", {"budget": {"lines_merge": 10}}, actor="human", via="api")
    assert proj.kind == "ignored"
    assert proj.charter.budget.lines_merge == 10


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


# ------------------------------------------- steward state / null sub-documents
#
# MongoDB cannot create a field under a NULL parent. Both writers of the steward
# sub-document address it with dotted paths, so persisting `steward: None` made
# every project created after 2026-08-15 permanently unusable by the steward and
# made its first tick raise. The 59 legacy rows survived only because their
# parent is MISSING, not null — an accident, not a design.

def test_fake_mongo_models_the_null_parent_write_error():
    """Guard on the guard: if this fake ever goes back to auto-creating a null
    parent, every test below stops being able to see the production failure."""
    with pytest.raises(WriteError):
        _dotted_set({"steward": None}, "steward.paused_reason", "x")
    # missing parent auto-creates, exactly like the real server
    doc: dict = {}
    _dotted_set(doc, "steward.paused_reason", "x")
    assert doc == {"steward": {"paused_reason": "x"}}


@pytest.mark.asyncio
async def test_created_project_accepts_a_steward_write():
    """Every REST/MCP/ambient-extractor project goes through create_project."""
    db = _FakeDB()
    svc = PlanningService(db)
    await svc.create_project(ProjectCreateRequest(name="Fresh Thing"))

    stored = await db.projects.find_one({"slug": "fresh-thing"})
    assert stored["steward"] == {}, "a null parent makes the first steward tick raise"

    assert await svc.propose_pause("fresh-thing", "21 days idle") is True
    after = await svc.update_steward_state("fresh-thing", {"no_progress_streak": 2})
    assert after.steward.no_progress_streak == 2
    assert after.steward.paused_reason == "21 days idle"


@pytest.mark.asyncio
async def test_harvested_project_accepts_a_steward_write(monkeypatch):
    """The harvester discovering a repo is the other insert path."""
    db = _FakeDB()
    _patch_discovery(monkeypatch, "/home/ben/Development/fresh-thing")
    monkeypatch.setattr(harvest_mod, "_looks_like_project", lambda p: True)

    await harvest(db, roots=["/home/ben/Development"])

    assert (await db.projects.find_one({"slug": "fresh-thing"}))["steward"] == {}
    svc = PlanningService(db)
    assert await svc.propose_pause("fresh-thing", "budget exhausted") is True
    stored = await db.projects.find_one({"slug": "fresh-thing"})
    assert stored["steward"]["paused_reason"] == "budget exhausted"


@pytest.mark.asyncio
async def test_steward_write_heals_a_row_that_already_holds_null():
    """Rows written by the broken code are already in the collection; the first
    steward write must repair them rather than raise forever (they cannot be
    migrated away by a code fix alone)."""
    db = _FakeDB([_project_doc("legacy-null", steward=None)])
    svc = PlanningService(db)

    proj = await svc.update_steward_state("legacy-null", {"no_progress_streak": 1})

    assert proj.steward.no_progress_streak == 1
    assert await svc.propose_pause("legacy-null", "ladder exhausted") is True
    stored = await db.projects.find_one({"slug": "legacy-null"})
    assert stored["steward"]["paused_reason"] == "ladder exhausted"


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
async def test_active_set_blockers_name_the_failing_condition():
    """The set the steward iterates and the answer a human is given come from
    the same function, so 'why isn't ARIA working on this?' has an answer."""
    svc = PlanningService(_FakeDB())
    keeper = svc._project_from_doc(_project_doc("keeper", charter={"purpose": "ship it"}))
    assert active_set_blockers(keeper) == []

    outside = svc._project_from_doc(_project_doc("noise", kind="ignored", status="paused"))
    assert len(active_set_blockers(outside)) == 3
    assert any("kind=ignored" in b for b in active_set_blockers(outside))
    assert any("status=paused" in b for b in active_set_blockers(outside))
    assert any("purpose" in b for b in active_set_blockers(outside))


@pytest.mark.asyncio
async def test_active_projects_can_exclude_the_steward_stand_down():
    """propose_pause promises `steward.paused_reason` stops the steward
    iterating a project — but `status` is human-owned and stays `active`, so
    the definitional set still contains it. A worker that spends budget asks
    for the filtered set; a human listing gets the default."""
    chartered = {"purpose": "ship it"}
    db = _FakeDB([
        _project_doc("keeper", charter=dict(chartered)),
        _project_doc("stood-down", charter=dict(chartered),
                     steward={"paused_reason": "budget exhausted"}),
    ])
    svc = PlanningService(db)

    assert [p.slug for p in await svc.active_projects()] == ["keeper", "stood-down"]
    assert [p.slug for p in await svc.active_projects(include_stood_down=False)] == ["keeper"]


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
    # `steward` is an empty document, never null: MongoDB cannot create
    # `steward.<field>` under a null parent, and the steward writes nothing else.
    assert on_insert["charter"] is None and on_insert["steward"] == {}
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
async def test_charter_route_reports_whether_the_steward_will_see_it(client):
    """A 200 that said nothing about the active set is how a charter on an
    ignored row looked identical to a charter on a live project."""
    resp = await client.put(
        "/api/v1/projects/aria/charter", json={"charter": {"budget": {"lines_merge": 10}}}
    )
    body = resp.json()
    assert body["in_active_set"] is False
    assert any("purpose" in b for b in body["active_set_blockers"])

    resp = await client.put(
        "/api/v1/projects/aria/charter", json={"charter": {"purpose": "Be the steward"}}
    )
    body = resp.json()
    assert body["in_active_set"] is True and body["active_set_blockers"] == []


@pytest.mark.asyncio
async def test_charter_route_409s_on_a_human_ignored_row(client):
    """`noise` is kind=ignored by a human decision. The old code answered 200
    with the charter echoed and the steward never looked at the row again."""
    client.db.projects.docs[1]["source"] = {"kind": {"actor": "human"}}

    resp = await client.put(
        "/api/v1/projects/noise/charter", json={"charter": {"purpose": "sneak it in"}}
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "charter_refused" and detail["project"] == "noise"
    assert "kind=project" in detail["remedy"]
    assert (await client.db.projects.find_one({"slug": "noise"})).get("charter") is None


@pytest.mark.asyncio
async def test_patch_can_settle_the_kind_and_the_charter_in_one_call(client):
    """The remedy the 409 hands back has to actually work: `kind` lands before
    the charter, so one PATCH resolves the contradiction."""
    client.db.projects.docs[1]["source"] = {"kind": {"actor": "human"}}

    resp = await client.patch(
        "/api/v1/projects/noise",
        json={"kind": "project", "charter": {"purpose": "actually a project"}},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "project"
    assert resp.json()["charter"]["purpose"] == "actually a project"


@pytest.mark.asyncio
async def test_charter_route_promotes_a_glob_ignored_row(client):
    """`noise` with no `source` provenance is a glob verdict, not a human one."""
    resp = await client.put(
        "/api/v1/projects/noise/charter", json={"charter": {"purpose": "real work"}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "project"
    assert resp.json()["in_active_set"] is True


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
