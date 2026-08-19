"""Tests for the StewardWorker — the per-project charter → gap → action loop.

The invariants under test are the ones that would cost Ben something real if
they broke, not stylistic ones:

- **Zero charters is today's state.** A tick with an empty active set must not
  call the model, write the vault, or record a run.
- **Autonomy is a gate, not a hint.** A1 never spawns a session; A2 without a
  vault approval degrades to proposing; a local tier caps at A2 whatever the
  charter says (D2).
- **An empty completion is a failure.** Qwen3.8 emits `reasoning_content`
  before `content`, so a short budget returns `content=""` — writing that as an
  answer is exactly how DS4 labelled every memory with zero entities.
- **Ben's edit always wins.** A vault charter/autonomy edit is applied as
  `actor="human", via="vault"`; a pause is proposed, never applied.
- **His notes are never overwritten.** The plan body must not re-emit the
  `## Notes from Ben` heading once the doc has one.

No network, no Mongo, no live aria-api. `signal_rpc`'s transport is nailed shut
for the whole module: corsair's .env carries a live break-glass account and the
signal-cli daemon really is listening, so an unpatched path here sends Ben an
actual Signal message.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bson import ObjectId

from aria.config import settings
from aria.integrations.obsidian import NOTES_HEADING
from aria.notifications import signal_rpc
from aria.planning.models import (
    Charter,
    CharterBudget,
    CharterGuard,
    Project,
    Task,
    TaskSource,
)
from aria.steward.service import (
    AUTONOMY_NAMES,
    PLANS_COLLECTION,
    RUNS_COLLECTION,
    StewardModelError,
    StewardWorker,
    extract_json_object,
)


# ---------------------------------------------------------------------------
# Minimal in-memory Mongo stand-in (no mongomock in this venv)
# ---------------------------------------------------------------------------

def _match(doc: dict, flt: dict) -> bool:
    for key, expected in (flt or {}).items():
        if key == "$or":
            if not any(_match(doc, sub) for sub in expected):
                return False
            continue
        actual = doc.get(key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if op == "$ne":
                    if actual == operand:
                        return False
                elif op == "$in":
                    if actual not in operand:
                        return False
                elif op == "$gte":
                    if actual is None or actual < operand:
                        return False
                elif op == "$regex":
                    if actual is None or not re.search(operand, str(actual)):
                        return False
                else:  # pragma: no cover - unsupported operator in a test
                    raise NotImplementedError(op)
        elif actual != expected:
            return False
    return True


def _apply(doc: dict, update: dict) -> None:
    for op, fields in update.items():
        if op == "$set":
            doc.update(fields)
        elif op == "$setOnInsert":
            for field, value in fields.items():
                doc.setdefault(field, value)
        elif op == "$inc":
            for field, delta in fields.items():
                doc[field] = (doc.get(field) or 0) + delta
        else:  # pragma: no cover
            raise NotImplementedError(op)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction=1):
        self._docs.sort(key=lambda d: (d.get(field) is None, d.get(field)), reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[: int(n)]
        return self

    async def to_list(self, length=None):
        return self._docs if length is None else self._docs[:length]


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc):
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def find_one(self, flt=None, *args, **kwargs):
        for doc in self.docs:
            if _match(doc, flt or {}):
                return dict(doc)
        return None

    async def update_one(self, flt, update, upsert=False, **kwargs):
        for doc in self.docs:
            if _match(doc, flt):
                _apply(doc, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            doc = {k: v for k, v in flt.items() if not isinstance(v, dict)}
            _apply(doc, update)
            self.docs.append(doc)
            return SimpleNamespace(matched_count=0, modified_count=0)
        return SimpleNamespace(matched_count=0, modified_count=0)

    def find(self, flt=None, *args, **kwargs):
        return _FakeCursor([dict(d) for d in self.docs if _match(d, flt or {})])

    async def count_documents(self, flt=None):
        return len([d for d in self.docs if _match(d, flt or {})])

    async def create_index(self, *args, **kwargs):
        return "idx"


class FakeDB:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, FakeCollection())


# ---------------------------------------------------------------------------
# Stand-ins for the collaborators the worker calls into
# ---------------------------------------------------------------------------

class FakePlanning:
    """Records what the steward asked planning to do, and with what actor."""

    def __init__(self, projects=None, tasks=None):
        self.projects = list(projects or [])
        self.tasks = list(tasks or [])
        self.created_tasks: list = []
        self.charter_calls: list[dict] = []
        self.pause_calls: list[tuple] = []
        self.activity: list[tuple] = []
        self.steward_state: list[tuple] = []
        self.open_task_by_hash = None

    async def active_projects(self):
        return [
            p for p in self.projects
            if p.status == "active" and p.kind == "project"
            and p.charter and p.charter.purpose.strip()
        ]

    async def list_projects(self, *, status=None):
        return list(self.projects)

    async def list_tasks(self, *, status=None, project_id=None, limit=200):
        return [t for t in self.tasks if project_id in (None, t.project_id)][:limit]

    async def get_project_by_slug(self, slug):
        return next((p for p in self.projects if p.slug == slug), None)

    async def get_project_by_ident(self, ident):
        return next((p for p in self.projects if p.slug == ident or p.id == ident), None)

    async def fuzzy_find_project(self, hint):
        return None

    async def find_open_task_by_hash(self, content_hash):
        return self.open_task_by_hash

    async def create_task(self, body, *, source=None):
        task = Task(
            id=f"{len(self.created_tasks):024d}",
            title=body.title,
            notes=body.notes,
            status=body.status,
            project_id=body.project_id,
            tags=list(body.tags),
            source=source or TaskSource(type="manual"),
            content_hash="x" * 64,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.created_tasks.append(task)
        return task

    async def set_charter(self, ident, charter, *, actor="human", via="api"):
        self.charter_calls.append(
            {"ident": ident, "charter": charter, "actor": actor, "via": via}
        )
        return await self.get_project_by_slug(ident)

    async def propose_pause(self, ident, reason):
        self.pause_calls.append((ident, reason))
        return True

    async def append_project_activity(self, project_id, *, source, note):
        self.activity.append((project_id, source, note))
        return True

    async def update_steward_state(self, ident, patch):
        self.steward_state.append((ident, patch))
        return await self.get_project_by_slug(ident)


class FakeAdapter:
    def __init__(self, content: str, usage=None, raises=None):
        self.content = content
        self.usage = usage or {"input_tokens": 100, "output_tokens": 50}
        self.raises = raises
        self.calls: list[dict] = []

    async def complete(self, messages, tools=None, temperature=0.7, max_tokens=4096):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if self.raises:
            raise self.raises
        return self.content, [], self.usage


class FakeLLMManager:
    def __init__(self, adapter):
        self.adapter = adapter
        self.calls: list[tuple] = []

    def get_adapter(self, backend, model, base_url=None):
        self.calls.append((backend, model, base_url))
        return self.adapter


class FakeGuard:
    def __init__(self):
        self.calls: list[dict] = []

    async def prepare_session(self, repo, session_id, project_slug=None):
        self.calls.append(
            {"repo": repo, "session_id": session_id, "project_slug": project_slug}
        )
        return {
            "worktree": f"{repo}/.worktrees/{project_slug}-{session_id[-4:]}",
            "branch": f"aria/{project_slug}/{session_id[-4:]}",
            "start_tag": f"aria/ckpt/{session_id}/start",
        }


class FakeCodingManager:
    def __init__(self):
        self.calls: list[dict] = []

    async def start_session(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "id": ObjectId(),
            "agent_conversation_id": "conv-1",
            "status": "running",
        }


class FakeNotifier:
    def __init__(self):
        self.sent: list[dict] = []

    async def notify(self, **kwargs):
        self.sent.append(kwargs)
        return {"queued": True}


class FakeShells:
    async def fleet_overview(self):
        return []


@pytest.fixture(autouse=True)
def _no_real_signal():
    """Nothing in this module may reach signal-cli. See the module docstring."""
    class _Exploding:
        def __init__(self, *a, **k):
            raise AssertionError("a steward test must not open a Signal connection")

    with patch.object(signal_rpc, "httpx", SimpleNamespace(AsyncClient=_Exploding)):
        yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _charter(**kwargs) -> Charter:
    base = {
        "purpose": "Keep ARIA's control plane coherent and safe to run unattended.",
        "goals": ["ship the guard", "keep the fleet green"],
        "success_criteria": ["zero destructive events", "gate green on 20 sessions"],
        "non_goals": ["rewriting Hermes"],
        "autonomy": 1,
        "tiers_allowed": ["local"],
        "budget": CharterBudget(),
        "guard": CharterGuard(allowed_paths=["api/aria/**"]),
    }
    base.update(kwargs)
    return Charter(**base)


def _project(tmp_path, *, charter=None, last_activity=None, steward=None) -> Project:
    now = datetime.now(timezone.utc)
    repo = tmp_path / "repos" / "ProjectAria"
    repo.mkdir(parents=True, exist_ok=True)
    return Project(
        id="222222222222222222222222",
        name="ProjectAria",
        slug="projectaria",
        summary="The control plane",
        status="active",
        kind="project",
        charter=charter if charter is not None else _charter(),
        steward=steward,
        relevant_paths=[],
        path=str(repo),
        check_command="make check",
        created_at=now,
        updated_at=now,
        last_signal_at=last_activity or now,
        last_activity_at=last_activity or now,
        git={"branch": "main", "last_commit_at": now},
    )


@pytest.fixture(autouse=True)
def _sandbox_vault(tmp_path):
    """No test in this module may reach Ben's real vault.

    It happened once while this file was being written: `ObsidianWriter` binds
    `settings.obsidian_vault_path` at CONSTRUCTION, so a worker built before the
    patch was entered wrote a real `STEWARD_PLAN.md` (and an `.aria-proposed.md`
    sibling) into ~/Obsidian/vault, which LiveSync would have pushed to his
    phone. Patching for the whole module — around fixture setup as well as the
    test body — is the only version of this that cannot be got wrong again.
    """
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    with (
        patch.object(settings, "obsidian_enabled", True),
        patch.object(settings, "obsidian_vault_path", str(vault)),
    ):
        yield vault


def _worker(db, planning, *, adapter=None, guard=None, coding=None, notifier=None):
    return StewardWorker(
        db,
        planning=planning,
        notifier=notifier or FakeNotifier(),
        llm_manager=FakeLLMManager(adapter or FakeAdapter('{"assessment":"a","gap":"g","actions":[]}')),
        guard=guard or FakeGuard(),
        coding_manager=coding or FakeCodingManager(),
        shell_service=FakeShells(),
    )


def _model_reply(*actions) -> str:
    import json

    return json.dumps({"assessment": "steady", "gap": "the gate is red", "actions": list(actions)})


def _plan_file(tmp_path, project, approval: str):
    path = (
        tmp_path / "vault" / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: plan\napproval: {approval}\n---\n\n# plan\n\nbody\n", encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# JSON extraction — a reasoning model does not answer in bare JSON
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_bare_object(self):
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_fenced_and_prefixed(self):
        text = 'Sure, here is the plan:\n```json\n{"actions": [{"kind": "note"}]}\n```\n'
        assert extract_json_object(text)["actions"][0]["kind"] == "note"

    def test_nested_braces_and_trailing_prose(self):
        text = '{"a": {"b": "}"}, "c": 2} — that is my answer'
        assert extract_json_object(text) == {"a": {"b": "}"}, "c": 2}

    def test_empty_content_is_an_error(self):
        # The DS4 bug: an empty completion must never read as "no actions".
        with pytest.raises(StewardModelError):
            extract_json_object("")

    def test_no_json_is_an_error(self):
        with pytest.raises(StewardModelError):
            extract_json_object("I think we should ship it.")


# ---------------------------------------------------------------------------
# The empty active set — today's real state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_with_no_chartered_projects_costs_nothing(tmp_path):
    db = FakeDB()
    planning = FakePlanning(projects=[
        # An uncharted harvested row: on disk, in the registry, not a project
        # the steward may touch.
        _project(tmp_path, charter=Charter(purpose="")),
    ])
    adapter = FakeAdapter(_model_reply({"kind": "note", "title": "x"}))
    worker = _worker(db, planning, adapter=adapter)

    result = await worker.tick()

    assert result["projects"] == 0
    assert result["reason"] == "no chartered projects"
    assert adapter.calls == []                      # no model call
    assert db[RUNS_COLLECTION].docs == []           # no run rows
    assert not (tmp_path / "vault" / "ProjectAria").exists()  # no vault write


@pytest.mark.asyncio
async def test_worker_does_not_start_when_disabled():
    worker = StewardWorker(FakeDB(), planning=FakePlanning())
    with patch.object(settings, "steward_enabled", False):
        await worker.start()
    assert worker._task is None


# ---------------------------------------------------------------------------
# Autonomy gating
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a1_proposes_a_task_and_refuses_a_session(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=1))
    planning = FakePlanning(projects=[project])
    coding = FakeCodingManager()
    adapter = FakeAdapter(_model_reply(
        {"kind": "task", "title": "Wire the merge gate into the watchdog", "why": "goal 1"},
        {"kind": "session", "title": "Do it now", "prompt": "go"},
    ))
    worker = _worker(db, planning, adapter=adapter, coding=coding)

    with patch.object(settings, "steward_max_actions_per_tick", 2):
        run = await worker.tick_project(project)

    assert run["autonomy_effective"] == 1
    assert [a["kind"] for a in run["actions_executed"]] == ["task"]
    assert planning.created_tasks[0].status == "proposed"
    assert coding.calls == []
    assert any(s["kind"] == "session" and "not permitted" in s["reason"] for s in run["skipped"])


@pytest.mark.asyncio
async def test_a2_without_vault_approval_degrades_to_proposing(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=2))
    planning = FakePlanning(projects=[project])
    coding = FakeCodingManager()
    adapter = FakeAdapter(_model_reply({"kind": "session", "title": "Fix the gate", "prompt": "go"}))
    worker = _worker(db, planning, adapter=adapter, coding=coding)

    run = await worker.tick_project(project)

    assert run["autonomy"] == 2 and run["autonomy_effective"] == 1
    assert coding.calls == []
    plan = (tmp_path / "vault" / "ProjectAria" / "Planning" / "STEWARD_PLAN.md").read_text()
    assert "approval: pending" in plan          # seeded, so there is a key to flip
    assert "Set `approval: approved`" in plan   # and the ask is stated


@pytest.mark.asyncio
async def test_a2_with_approval_starts_a_session_through_the_guard(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=2))
    planning = FakePlanning(projects=[project])
    guard, coding = FakeGuard(), FakeCodingManager()
    adapter = FakeAdapter(_model_reply(
        {"kind": "session", "title": "Fix the red gate", "why": "criterion 2", "prompt": "Fix it."}
    ))
    worker = _worker(db, planning, adapter=adapter, guard=guard, coding=coding)

    _plan_file(tmp_path, project, "approved")
    run = await worker.tick_project(project)

    assert run["autonomy_effective"] == 2
    assert guard.calls[0]["repo"] == project.path
    assert guard.calls[0]["project_slug"] == "projectaria"
    call = coding.calls[0]
    # The session runs in the guard's worktree, never in the live checkout, and
    # never provisions its own — the guard holds the pen.
    assert call["workspace"] == guard.calls[0]["repo"] + "/.worktrees/projectaria-" + \
        guard.calls[0]["session_id"][-4:]
    assert call["create_worktree"] is False
    assert call["backend"] == "pi-code"
    assert "Do NOT merge" in call["prompt"]
    assert run["actions_executed"][0]["kind"] == "session"


@pytest.mark.asyncio
async def test_a_guard_failure_stops_the_session_not_the_tick(tmp_path):
    """No worktree means no session: the live checkout is never the fallback."""
    class BrokenGuard(FakeGuard):
        async def prepare_session(self, repo, session_id, project_slug=None):
            raise RuntimeError("could not create worktree: repo is bare")

    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=2))
    coding = FakeCodingManager()
    worker = _worker(
        db, FakePlanning(projects=[project]), guard=BrokenGuard(), coding=coding,
        adapter=FakeAdapter(_model_reply({"kind": "session", "title": "go", "prompt": "go"})),
    )

    _plan_file(tmp_path, project, "approved")
    run = await worker.tick_project(project)

    assert coding.calls == []
    assert run["actions_executed"] == []
    assert "could not create worktree" in run["skipped"][0]["reason"]
    assert run["status"] == "observing"       # the tick itself completed


@pytest.mark.asyncio
async def test_local_tier_caps_autonomy_at_a2(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=3, tiers_allowed=["local"]))
    planning = FakePlanning(projects=[project])
    worker = _worker(db, planning, adapter=FakeAdapter(_model_reply()))

    _plan_file(tmp_path, project, "approved")
    run = await worker.tick_project(project)

    # A3 is cloud-only (D2): a local model may execute in a worktree, never merge.
    assert run["autonomy"] == 3
    assert run["autonomy_effective"] == 2
    assert run["autonomy_label"] == AUTONOMY_NAMES[2]


@pytest.mark.asyncio
async def test_a0_makes_no_model_call(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=0))
    planning = FakePlanning(projects=[project])
    adapter = FakeAdapter(_model_reply({"kind": "task", "title": "nope"}))
    worker = _worker(db, planning, adapter=adapter)

    run = await worker.tick_project(project)

    assert adapter.calls == []
    assert run["actions_executed"] == []
    assert run["status"] == "observing"


# ---------------------------------------------------------------------------
# The model failing is not the model saying "nothing"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_completion_is_a_failure_not_an_empty_plan(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=1))
    planning = FakePlanning(projects=[project])
    worker = _worker(db, planning, adapter=FakeAdapter(""))

    run = await worker.tick_project(project)

    assert run["model"]["ok"] is False
    assert "empty content" in run["model"]["error"]
    assert run["status"] == "model-failed"
    assert run["actions_executed"] == []
    plan = (tmp_path / "vault" / "ProjectAria" / "Planning" / "STEWARD_PLAN.md").read_text()
    # The doc must say the assessment did not happen — never "no gaps found".
    assert "could not assess" in plan
    assert planning.created_tasks == []


@pytest.mark.asyncio
async def test_model_gets_a_generous_token_budget(tmp_path):
    """A tight budget is why the reply comes back empty in the first place."""
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=1))
    adapter = FakeAdapter(_model_reply())
    worker = _worker(db, FakePlanning(projects=[project]), adapter=adapter)

    await worker.tick_project(project)

    assert adapter.calls[0]["max_tokens"] == settings.steward_max_tokens
    assert settings.steward_max_tokens >= 512


@pytest.mark.asyncio
async def test_model_junk_is_discarded_not_executed(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=1))
    planning = FakePlanning(projects=[project])
    adapter = FakeAdapter(_model_reply(
        {"kind": "merge", "title": "merge to main"},   # not an action kind
        {"kind": "task", "title": ""},                  # no title
    ))
    worker = _worker(db, planning, adapter=adapter)

    run = await worker.tick_project(project)

    assert run["actions_executed"] == []
    assert {s["reason"] for s in run["skipped"]} == {"unknown action kind", "no title"}
    assert planning.created_tasks == []


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exhausted_session_budget_proposes_instead_of_acting(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=2))
    planning = FakePlanning(projects=[project])
    coding = FakeCodingManager()
    now = datetime.now(timezone.utc)
    for _ in range(settings.steward_default_sessions_per_day):
        await db[RUNS_COLLECTION].insert_one({
            "slug": "projectaria",
            "started_at": now - timedelta(hours=1),
            "actions_executed": [{"kind": "session", "title": "earlier work"}],
        })
    adapter = FakeAdapter(_model_reply({"kind": "session", "title": "more", "prompt": "go"}))
    worker = _worker(db, planning, adapter=adapter, coding=coding)

    _plan_file(tmp_path, project, "approved")
    run = await worker.tick_project(project)

    assert run["budget"]["sessions_remaining"] == 0
    assert "session" not in run["allowed_kinds"]
    assert coding.calls == []
    assert run["status"] == "budget-exhausted"


@pytest.mark.asyncio
async def test_budget_ledger_counts_only_the_stewards_own_sessions(tmp_path):
    """Ben's own coding day must not spend the steward's budget, and vice versa."""
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=2))
    await db.coding_sessions.insert_one({
        "workspace": project.path, "status": "running",
        "updated_at": datetime.now(timezone.utc),
    })
    worker = _worker(db, FakePlanning(projects=[project]))

    run = await worker.tick_project(project)

    assert run["budget"]["sessions_today"] == 0
    assert run["budget"]["cloud_usd_today"] == 0.0  # it spent nothing, so it owes nothing


@pytest.mark.asyncio
async def test_unpriceable_cloud_spend_reads_as_unknown_not_zero(tmp_path):
    """Usage accounting has recorded nothing since 2026-07-30. An unanswerable
    cost must not come back as $0.00 — that would silently retire the cap."""
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=2))
    await db[RUNS_COLLECTION].insert_one({
        "slug": "projectaria",
        "started_at": datetime.now(timezone.utc) - timedelta(minutes=30),
        "actions_executed": [{"kind": "session", "conversation_id": "conv-1"}],
    })
    worker = _worker(db, FakePlanning(projects=[project]))

    run = await worker.tick_project(project)  # FakeDB has no aggregate()

    assert run["budget"]["cloud_measured"] is False
    assert run["budget"]["cloud_exhausted"] is False
    plan = (tmp_path / "vault" / "ProjectAria" / "Planning" / "STEWARD_PLAN.md").read_text()
    assert "unmeasured" in plan


# ---------------------------------------------------------------------------
# Pause proposals and the plan document
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_project_pause_is_proposed_never_applied(tmp_path):
    db = FakeDB()
    stale = datetime.now(timezone.utc) - timedelta(
        days=settings.steward_idle_days_before_pause_proposal + 5
    )
    project = _project(tmp_path, charter=_charter(autonomy=1), last_activity=stale)
    planning = FakePlanning(projects=[project])
    notifier = FakeNotifier()
    worker = _worker(db, planning, adapter=FakeAdapter(_model_reply()), notifier=notifier)

    run = await worker.tick_project(project)

    assert planning.pause_calls and planning.pause_calls[0][0] == "projectaria"
    assert project.status == "active"          # lifecycle stays human-owned
    assert run["pause_proposal"]["proposed"] is True
    raised = [n for n in notifier.sent if n["event_type"] == "pause_proposed"]
    assert raised and raised[0]["needs_human"] is True


@pytest.mark.asyncio
async def test_a_stood_down_project_is_skipped_cheaply(tmp_path):
    db = FakeDB()
    project = _project(
        tmp_path, charter=_charter(autonomy=2),
        steward={"enabled": True, "paused_reason": "idle 30 days; pause proposed"},
    )
    adapter = FakeAdapter(_model_reply({"kind": "task", "title": "x"}))
    worker = _worker(db, FakePlanning(projects=[project]), adapter=adapter)

    run = await worker.tick_project(project)

    assert run["status"] == "standing-down"
    assert adapter.calls == []
    assert not (tmp_path / "vault" / "ProjectAria" / "Planning").exists()


@pytest.mark.asyncio
async def test_plan_does_not_re_emit_bens_notes_heading(tmp_path):
    """Emitting the heading into a doc that already has one makes
    upsert_managed skip its preservation branch — Ben's notes would vanish."""
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=1))
    worker = _worker(db, FakePlanning(projects=[project]), adapter=FakeAdapter(_model_reply()))

    run_first = await worker.tick_project(project)
    assert NOTES_HEADING in (tmp_path / "vault" / "ProjectAria" / "Planning" /
                             "STEWARD_PLAN.md").read_text()
    observed = await worker._observe(project, await worker._shared_context([project]))
    body = worker._plan_body(
        project, project.charter, run_first, observed, run_first["budget"]
    )

    assert observed["plan"]["has_notes_section"] is True
    assert NOTES_HEADING not in body


@pytest.mark.asyncio
async def test_an_unchanged_plan_is_not_rewritten(tmp_path):
    """48 ticks a day must not be 48 LiveSync versions of the same document —
    and on a doc Ben has edited, 48 identical `.aria-proposed.md` siblings."""
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=1))
    worker = _worker(db, FakePlanning(projects=[project]), adapter=FakeAdapter(_model_reply()))

    first = await worker.tick_project(project)
    # The second tick still writes: the first created the `## Notes from Ben`
    # stub, so from now on the body omits that heading (and the writer preserves
    # the section instead). It settles from there.
    second = await worker.tick_project(project)
    plan = tmp_path / "vault" / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
    stamp = plan.stat().st_mtime_ns
    third = await worker.tick_project(project)

    assert first["plan_write"]["wrote"] is True
    assert second["plan_write"]["wrote"] is True
    assert third["plan_write"]["reason"] == "unchanged"
    assert plan.stat().st_mtime_ns == stamp
    assert NOTES_HEADING in plan.read_text()


@pytest.mark.asyncio
async def test_run_and_state_are_recorded(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=1))
    planning = FakePlanning(projects=[project])
    worker = _worker(db, planning, adapter=FakeAdapter(_model_reply(
        {"kind": "note", "title": "gate has been red for two days"}
    )))

    await worker.tick_project(project)

    assert len(db[RUNS_COLLECTION].docs) == 1
    assert db[RUNS_COLLECTION].docs[0]["slug"] == "projectaria"
    assert db[PLANS_COLLECTION].docs[0]["_id"] == "projectaria"
    ident, patch_fields = planning.steward_state[0]
    assert ident == "projectaria"
    assert set(patch_fields) <= {
        "enabled", "last_run_at", "plan_hash", "last_report_ref", "no_progress_streak"
    }
    assert planning.activity and planning.activity[0][1] == "steward"


@pytest.mark.asyncio
async def test_quiet_tick_does_not_flood_the_activity_ring(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=1))
    planning = FakePlanning(projects=[project])
    worker = _worker(db, planning, adapter=FakeAdapter(_model_reply()))

    await worker.tick_project(project)

    assert planning.activity == []  # the ring holds 20 entries; 48 ticks/day


# ---------------------------------------------------------------------------
# Vault events — Ben's edit always wins
# ---------------------------------------------------------------------------

def _event(kind, **kwargs):
    base = {
        "type": kind,
        "path": "/vault/ProjectAria/Planning/CHARTER.md",
        "rel_path": "ProjectAria/Planning/CHARTER.md",
        "project": "ProjectAria",
        "doc": "charter",
        "at": datetime.now(timezone.utc),
    }
    base.update(kwargs)
    return base


@pytest.mark.asyncio
async def test_vault_charter_is_applied_as_a_human_edit(tmp_path):
    db = FakeDB()
    project = _project(tmp_path)
    planning = FakePlanning(projects=[project])
    worker = _worker(db, planning)

    out = await worker.handle_vault_events([
        _event("charter", value={
            "purpose": "New purpose from the phone",
            "goals": ["a", "b"],
            "title": "ignored non-charter key",
        })
    ])

    assert out["results"][0]["action"] == "charter_applied"
    call = planning.charter_calls[0]
    assert call["actor"] == "human" and call["via"] == "vault"
    assert set(call["charter"]) == {"purpose", "goals"}  # non-charter keys filtered


@pytest.mark.asyncio
async def test_vault_autonomy_flip_updates_the_charter(tmp_path):
    db = FakeDB()
    project = _project(tmp_path)
    planning = FakePlanning(projects=[project])
    worker = _worker(db, planning)

    await worker.handle_vault_events([_event("autonomy", value=2, doc="steward_plan")])

    assert planning.charter_calls[0]["charter"] == {"autonomy": 2}
    assert planning.charter_calls[0]["actor"] == "human"


@pytest.mark.asyncio
async def test_vault_autonomy_out_of_range_is_rejected(tmp_path):
    db = FakeDB()
    planning = FakePlanning(projects=[_project(tmp_path)])
    worker = _worker(db, planning)

    out = await worker.handle_vault_events([_event("autonomy", value=9)])

    assert out["results"][0]["action"] == "rejected"
    assert planning.charter_calls == []


@pytest.mark.asyncio
async def test_vault_approval_is_recorded_and_gates_the_next_tick(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=2))
    planning = FakePlanning(projects=[project])
    coding = FakeCodingManager()
    worker = _worker(
        db, planning, coding=coding,
        adapter=FakeAdapter(_model_reply({"kind": "session", "title": "go", "prompt": "go"})),
    )

    await worker.handle_vault_events([_event("approval", value="approved", doc="steward_plan")])
    assert db[PLANS_COLLECTION].docs[0]["approval"] == "approved"

    # The FILE is what the tick reads, not the mirror — a missed event must not
    # be able to authorise execution on its own.
    run_without_file = await worker.tick_project(project)
    assert run_without_file["autonomy_effective"] == 1
    assert coding.calls == []


@pytest.mark.asyncio
async def test_vault_accepted_updates_the_research_run(tmp_path):
    db = FakeDB()
    path = "/vault/ProjectAria/Research/2026-08-15 topic.md"
    await db.research_runs.insert_one({"_id": "r1", "vault_path": path, "query": "q"})
    worker = _worker(db, FakePlanning(projects=[_project(tmp_path)]))

    out = await worker.handle_vault_events([
        _event("accepted", value=True, path=path, rel_path="ProjectAria/Research/x.md",
               doc="research", frontmatter={"accepted": True})
    ])

    assert out["results"][0]["action"] == "research_accepted"
    assert db.research_runs.docs[0]["accepted"] is True
    assert db.research_runs.docs[0]["accepted_via"] == "vault"


@pytest.mark.asyncio
async def test_vault_notes_are_kept_for_the_next_prompt(tmp_path):
    db = FakeDB()
    project = _project(tmp_path)
    worker = _worker(db, FakePlanning(projects=[project]))

    await worker.handle_vault_events([
        _event("notes", value="Do NOT touch the guard this week.", doc="steward_plan")
    ])

    assert "Do NOT touch" in db[PLANS_COLLECTION].docs[0]["notes_from_ben"]


@pytest.mark.asyncio
async def test_unreadable_vault_doc_raises_to_ben(tmp_path):
    db = FakeDB()
    notifier = FakeNotifier()
    worker = _worker(db, FakePlanning(projects=[_project(tmp_path)]), notifier=notifier)

    out = await worker.handle_vault_events([
        _event("parse_error", error="line 4: unterminated flow list")
    ])

    assert out["results"][0]["action"] == "raised"
    alert = notifier.sent[0]
    # A dropped edit is a decision Ben thinks he made — this is one of the few
    # steward events that is allowed to reach him.
    assert alert["needs_human"] is True
    assert "NOT been applied" in alert["detail"]


@pytest.mark.asyncio
async def test_vault_event_for_an_unknown_project_is_filed_not_dropped(tmp_path):
    db = FakeDB()
    worker = _worker(db, FakePlanning(projects=[_project(tmp_path)]))

    out = await worker.handle_vault_events([
        _event("charter", project="SomeOtherRepo", value={"purpose": "p"})
    ])

    assert out["results"][0]["action"] == "unmatched"
    assert db.scan_review.docs[0]["kind"] == "steward_vault_unmatched"


@pytest.mark.asyncio
async def test_one_bad_event_does_not_stop_the_rest(tmp_path):
    db = FakeDB()
    project = _project(tmp_path)
    planning = FakePlanning(projects=[project])
    worker = _worker(db, planning)

    out = await worker.handle_vault_events([
        {"type": "charter"},  # no project key at all
        _event("autonomy", value=1, doc="steward_plan"),
        {"type": "something_new"},
    ])

    assert out["handled"] == 3
    assert planning.charter_calls[-1]["charter"] == {"autonomy": 1}
    assert out["results"][2]["action"] == "ignored"


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_says_so_when_nothing_is_chartered(tmp_path):
    worker = _worker(FakeDB(), FakePlanning(projects=[]))
    status = await worker.status()
    assert status["active_projects"] == []
    assert "No chartered projects" in status["note"]
    assert status["model"]["model"] == settings.steward_model


@pytest.mark.asyncio
async def test_status_lists_the_active_set(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=2))
    worker = _worker(db, FakePlanning(projects=[project]))
    await db[PLANS_COLLECTION].update_one(
        {"_id": "projectaria"}, {"$set": {"approval": "approved"}}, upsert=True
    )

    status = await worker.status()

    row = status["active_projects"][0]
    assert row["slug"] == "projectaria"
    assert row["autonomy"] == 2 and row["approval"] == "approved"
    assert row["budget"]["sessions_per_day"] == settings.steward_default_sessions_per_day


@pytest.mark.asyncio
async def test_resume_clears_the_stand_down(tmp_path):
    db = FakeDB()
    project = _project(tmp_path, steward={"enabled": True, "paused_reason": "idle"})
    planning = FakePlanning(projects=[project])
    worker = _worker(db, planning)

    result = await worker.resume("projectaria")

    assert result == {"slug": "projectaria", "paused_reason": None}
    assert planning.steward_state[0] == ("projectaria", {"paused_reason": None})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
async def client(tmp_path, _sandbox_vault):
    # `_sandbox_vault` is requested explicitly, not just autouse: this fixture
    # CONSTRUCTS the worker (and its ObsidianWriter), and the writer binds the
    # vault path at construction — so the sandbox has to be in place first.
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from aria.api import deps
    from aria.api.routes import steward as steward_routes

    db = FakeDB()
    project = _project(tmp_path, charter=_charter(autonomy=1))
    planning = FakePlanning(projects=[project])
    worker = _worker(db, planning, adapter=FakeAdapter(_model_reply()))

    app = FastAPI()
    app.include_router(steward_routes.router, prefix="/api/v1")
    app.dependency_overrides[deps.get_db] = lambda: db
    app.dependency_overrides[deps.get_planning_service] = lambda: planning
    app.state.steward = worker

    with patch.object(settings, "admin_key", "test-admin-key"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            ac.worker = worker          # type: ignore[attr-defined]
            ac.planning = planning      # type: ignore[attr-defined]
            ac.db = db                  # type: ignore[attr-defined]
            yield ac


@pytest.mark.asyncio
async def test_route_status(client):
    resp = await client.get("/api/v1/steward/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is settings.steward_enabled
    assert body["active_projects"][0]["slug"] == "projectaria"


@pytest.mark.asyncio
async def test_route_runs_is_empty_before_any_tick(client):
    resp = await client.get("/api/v1/steward/runs")
    assert resp.status_code == 200
    assert resp.json() == {"runs": [], "count": 0, "slug": None}


@pytest.mark.asyncio
async def test_route_tick_requires_the_admin_key(client):
    resp = await client.post("/api/v1/steward/projects/projectaria/tick", json={})
    assert resp.status_code == 403

    resp = await client.post(
        "/api/v1/steward/projects/projectaria/tick",
        json={"dry_run": True},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True


@pytest.mark.asyncio
async def test_route_tick_dry_run_writes_nothing(client):
    resp = await client.post(
        "/api/v1/steward/projects/projectaria/tick",
        json={"dry_run": True},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    assert client.db[RUNS_COLLECTION].docs == []
    assert client.planning.created_tasks == []


@pytest.mark.asyncio
async def test_route_tick_404_and_409(client):
    resp = await client.post(
        "/api/v1/steward/projects/nope/tick", json={},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert resp.status_code == 404

    client.planning.projects[0].charter = Charter(purpose="   ")
    resp = await client.post(
        "/api/v1/steward/projects/projectaria/tick", json={},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert resp.status_code == 409
    assert "no charter purpose" in resp.json()["detail"]


class TestResearchNeedsAnApprovedPlan:
    """A1 may PROPOSE research topics; only an approved plan may RUN one.

    Ben, 2026-08-15. The distinction is proposal vs outward action: proposing a
    topic writes a line into a plan he reads, while running it reaches the
    public internet, spends a token budget and publishes an artifact.
    """

    def test_a1_without_approval_cannot_run_research(self):
        from aria.steward.service import StewardWorker

        w = StewardWorker.__new__(StewardWorker)
        budget = {"research_remaining": 5, "sessions_remaining": 5}
        allowed = w._allowed_kinds(1, budget, "local", approval=None)
        assert "research" not in allowed
        assert "task" in allowed          # proposing is still fine at A1
        assert "session" not in allowed

    def test_a1_with_approval_may_run_research(self):
        from aria.steward.service import StewardWorker

        w = StewardWorker.__new__(StewardWorker)
        budget = {"research_remaining": 5, "sessions_remaining": 5}
        allowed = w._allowed_kinds(1, budget, "local", approval="approved")
        assert "research" in allowed

    def test_an_exhausted_research_budget_still_wins(self):
        from aria.steward.service import StewardWorker

        w = StewardWorker.__new__(StewardWorker)
        budget = {"research_remaining": 0, "sessions_remaining": 5}
        allowed = w._allowed_kinds(2, budget, "local", approval="approved")
        assert "research" not in allowed


# ---------------------------------------------------------------------------
# Charter parsing: body prose and the guard/check_command keys (2026-08-19)
#
# `_on_charter` filtered the frontmatter to `Charter.model_fields` and ignored
# the body entirely, so three things Ben had actually written were dropped:
# `## Goals` prose, the top-level `allowed_paths`/`protected_paths`, and
# `check_command`. The visible symptom was the steward proposing "add explicit
# goals to the charter" against a charter that plainly had them; the invisible
# one was an empty per-project blast radius and a gate with nothing to run.
# ---------------------------------------------------------------------------

class TestCharterBodyAndGuardParsing:
    def test_bullets_parses_the_common_list_forms(self):
        from aria.steward.service import _bullets

        assert _bullets("- one\n- two") == ["one", "two"]
        assert _bullets("* one\n+ two\n1. three\n2) four") == ["one", "two", "three", "four"]
        assert _bullets(None) == []
        assert _bullets("just prose, no bullets") == []
        # ARIA's own drafts carry italic guidance under the heading; not a goal.
        assert _bullets("- real goal\n- _(drafted from CLAUDE.md)_") == ["real goal"]

    def test_body_sections_become_charter_lists(self):
        from aria.steward.service import CHARTER_BODY_SECTIONS, _bullets
        from aria.integrations.obsidian import extract_section

        body = (
            "# ProjectAria — charter\n\n"
            "## Purpose\n\nSome prose.\n\n"
            "## Goals\n\n- Be the control plane\n- Supervise every agent\n\n"
            "## Non-goals\n\n- Being a chat front door\n\n"
            "## Budget\n\nDefaults apply.\n"
        )
        parsed = {
            field: _bullets(extract_section(body, heading))
            for field, heading in CHARTER_BODY_SECTIONS.items()
        }
        assert parsed["goals"] == ["Be the control plane", "Supervise every agent"]
        assert parsed["non_goals"] == ["Being a chat front door"]
        assert parsed["success_criteria"] == []  # absent -> stays empty, not invented

    def test_frontmatter_wins_over_body(self):
        """Both present is not a conflict to resolve at read time — the typed
        field is the one Ben can see ARIA parsing, so it wins."""
        from aria.steward.service import CHARTER_BODY_SECTIONS, _bullets
        from aria.integrations.obsidian import extract_section

        patch = {"goals": ["from frontmatter"]}
        body = "## Goals\n\n- from the body\n"
        for field, heading in CHARTER_BODY_SECTIONS.items():
            if patch.get(field):
                continue
            items = _bullets(extract_section(body, heading))
            if items:
                patch[field] = items
        assert patch["goals"] == ["from frontmatter"]

    def test_guard_aliases_are_nested_not_dropped(self):
        from aria.steward.service import CHARTER_GUARD_ALIASES
        from aria.planning.models import Charter

        raw = {
            "purpose": "p",
            "allowed_paths": ["api/aria/**"],
            "protected_paths": ["api/tests/**"],
            "check_command": "cd api && pytest -q",
        }
        patch = {k: v for k, v in raw.items() if k in set(Charter.model_fields)}
        assert "allowed_paths" not in patch, "precondition: the old filter dropped these"

        guard_patch = {k: raw[k] for k in CHARTER_GUARD_ALIASES if k in raw}
        merged = dict(patch.get("guard") or {})
        merged.update(guard_patch)
        patch["guard"] = merged

        charter = Charter(**patch)
        assert charter.guard.allowed_paths == ["api/aria/**"]
        assert charter.guard.protected_paths == ["api/tests/**"]


class TestTaskOwner:
    def test_owner_coercion_fails_toward_human_review(self):
        from aria.steward.service import _task_owner

        assert _task_owner("human") == "human"
        assert _task_owner("Agent") == "agent"
        assert _task_owner(None) == "unknown"
        assert _task_owner("") == "unknown"
        assert _task_owner("robot") == "unknown"
        assert _task_owner(True) == "unknown"

    def test_the_prompt_asks_for_an_owner(self):
        """The field only gets populated if the model is told to produce it."""
        from aria.steward import service

        prompt = service.ACTION_PROMPT if hasattr(service, "ACTION_PROMPT") else None
        source = (prompt or "") + open(service.__file__).read()
        assert '"owner": "<only for kind=task: agent|human>"' in source
        assert "OWNER (kind=task only)" in source
