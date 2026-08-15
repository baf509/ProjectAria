"""
ARIA - Improver tests (steward proposal §8)

What these lock down is not "the code runs" but the four properties that make an
eval-gated self-improvement loop safe to leave switched on:

  1. **The mutable surface is small, and reaching outside it is a RAISE.** Every
     documented self-improvement failure is the agent editing its own evaluator
     or stop button (DGM deleted its hallucination markers; the AI Scientist
     raised its own timeout; METR's o3 hacked RE-Bench harder when it could read
     the scorer). So a proposal naming `api/aria/guard/**`, `api/tests/**`,
     `config.py`, `.env` or `**/evalstack/**` must produce a critical
     `needs_human` alert and a blocked `guard_events` row — never a quiet
     rejection. And the answer must come from `guard.policy.is_protected()`, not
     from a second copy of the list that will drift permissive.
  2. **No baseline, no proposal; no evaluator, no promotion.** Under N labelled
     outcomes the tick does nothing and says so, without ever calling a model.
  3. **Every write has a version row, and that row is the undo.** `rollback()`
     restores `before` byte-for-byte; a drifted target refuses promotion rather
     than overwriting whoever edited it.
  4. **Auto-apply is earned and watched.** Promotions ask Ben until enough
     promotions of that class have SURVIVED a regression window; a regression
     inside the window rolls back automatically and raises.

Test discipline: no network, no Mongo, no live aria-api, no subprocess, and
nothing that could reach `aria.notifications.signal_rpc` (which really sends
Signal messages to Ben) — the notifier here is a recorder.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

import pytest
from httpx import ASGITransport, AsyncClient

from aria.config import settings
from aria.guard import policy as guard_policy
from aria.steward import improve
from aria.steward.improve import (
    KIND_AGENT_PROMPT,
    KIND_PROMPT_FILE,
    KIND_SKILL,
    KIND_THRESHOLD,
    STATUS_PROMOTED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    STATUS_ROLLED_BACK,
    FixtureEvaluator,
    Improver,
    ImproverError,
    NeedsHuman,
    PolicyVersionStore,
    Target,
    collect_baseline,
    extract_json,
    load_fixture,
    model_family,
    parse_target,
    scan_destructive,
    score_case,
    validate_target,
)


# ---------------------------------------------------------------------------
# Minimal in-memory Mongo stand-in (no mongomock in this venv — same shape as
# tests/test_alerts_v2.py)
# ---------------------------------------------------------------------------

def _match(doc: dict, flt: dict) -> bool:
    for key, expected in (flt or {}).items():
        actual = _dotted(doc, key)
        if isinstance(expected, dict) and any(k.startswith("$") for k in expected):
            for op, operand in expected.items():
                if op == "$ne" and actual == operand:
                    return False
                if op == "$in" and actual not in operand:
                    return False
                if op == "$gte" and (actual is None or actual < operand):
                    return False
                if op == "$lt" and (actual is None or actual >= operand):
                    return False
        elif actual != expected:
            return False
    return True


def _dotted(doc: dict, path: str):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_dotted(doc: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction=1):
        self._docs.sort(key=lambda d: (d.get(field) is None, d.get(field)),
                        reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[: int(n)]
        return self

    async def to_list(self, length=None):
        return [dict(d) for d in (self._docs if length is None else self._docs[:length])]


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = [dict(d) for d in (docs or [])]

    async def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", f"auto-{len(self.docs)}")
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def find_one(self, flt=None, projection=None):
        for doc in self.docs:
            if _match(doc, flt or {}):
                return dict(doc)
        return None

    def find(self, flt=None, projection=None):
        return _Cursor([d for d in self.docs if _match(d, flt or {})])

    async def update_one(self, flt, update, upsert=False, **kwargs):
        for doc in self.docs:
            if _match(doc, flt):
                for op, fields in update.items():
                    if op == "$set":
                        for k, v in fields.items():
                            _set_dotted(doc, k, v)
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            doc = {k: v for k, v in (flt or {}).items() if not isinstance(v, dict)}
            for op, fields in update.items():
                if op == "$set":
                    for k, v in fields.items():
                        _set_dotted(doc, k, v)
            self.docs.append(doc)
            return SimpleNamespace(matched_count=0, modified_count=1, upserted_id=doc.get("_id"))
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def delete_one(self, flt):
        for i, doc in enumerate(self.docs):
            if _match(doc, flt):
                self.docs.pop(i)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def count_documents(self, flt=None):
        return len([d for d in self.docs if _match(d, flt or {})])


class FakeDB:
    def __init__(self):
        self._collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name) -> FakeCollection:
        return self._collections.setdefault(name, FakeCollection())

    def __getattr__(self, name) -> FakeCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


class Notifier:
    """Records instead of sending. Must never be the real NotificationService:
    the outbox path ends at Ben's phone."""

    def __init__(self):
        self.sent: list[dict] = []

    async def notify(self, **kwargs):
        self.sent.append(kwargs)
        return {"queued": True}

    def of_kind(self, event_type: str) -> list[dict]:
        return [a for a in self.sent if a.get("event_type") == event_type]


@dataclass
class FakeGuard:
    """Stands in for GitGuard. We call `prepare_session`/`discard` — the real
    ones do live git operations, which a unit test must not."""

    worktree: str
    prepared: list[str] = None
    discarded: list[str] = None
    fail_prepare: bool = False

    def __post_init__(self):
        self.prepared = []
        self.discarded = []

    async def prepare_session(self, repo, session_id, project_slug=None):
        if self.fail_prepare:
            raise RuntimeError("worktree unavailable")
        self.prepared.append(session_id)
        return {"worktree": self.worktree, "session_id": session_id,
                "branch": f"aria/aria/{session_id[:8]}"}

    async def discard(self, session_id):
        self.discarded.append(session_id)
        return {"ok": True}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    return FakeDB()


@pytest.fixture
def cfg(monkeypatch):
    """Override settings the improver reads through `_setting`.

    Several of the knobs this component needs are not in `config.py` yet (and
    `config.py` is a protected path this component must not edit), so they are
    read with `getattr(settings, ..., default)`. This fixture overrides that
    single seam rather than mutating a pydantic Settings instance.
    """
    overrides: dict = {}
    real = improve._setting

    def fake(name, default):
        if name in overrides:
            return overrides[name]
        return real(name, default)

    monkeypatch.setattr(improve, "_setting", fake)
    return overrides


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway repo root: api/prompts (mutable) + api/tests (protected)."""
    (tmp_path / "api" / "prompts").mkdir(parents=True)
    (tmp_path / "api" / "tests" / "fixtures").mkdir(parents=True)
    (tmp_path / "api" / "prompts" / "steward.md").write_text("BEFORE prompt\n")
    monkeypatch.setattr(improve, "repo_root", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def worktree(repo, tmp_path):
    """A separate checkout, as `GitGuard.prepare_session` really produces.

    Pointing the fake guard at the repo root instead would let the gate's
    candidate write land on the live file — which is precisely the thing the
    worktree exists to prevent, and a test that could not tell the difference
    would be worthless.
    """
    wt = tmp_path / "worktree"
    (wt / "api" / "prompts").mkdir(parents=True)
    (wt / "api" / "prompts" / "steward.md").write_text(
        (repo / "api" / "prompts" / "steward.md").read_text()
    )
    return wt


@pytest.fixture
def store(db, repo):
    return PolicyVersionStore(db, str(repo))


def _fixture_file(repo, cfg, cases: list[dict]) -> str:
    import json

    path = repo / "api" / "tests" / "fixtures" / "improver_eval.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in cases))
    cfg["improver_eval_fixture"] = "api/tests/fixtures/improver_eval.jsonl"
    return str(path)


def _outcomes(db, n: int, successes: int, *, when: Optional[datetime] = None) -> None:
    when = when or datetime.now(timezone.utc) - timedelta(days=1)
    for i in range(n):
        db["session_outcomes"].docs.append({
            "_id": f"o{i}-{when.timestamp()}",
            "session_id": f"s{i}",
            "label": "success" if i < successes else "failed",
            "created_at": when,
        })


# ---------------------------------------------------------------------------
# 1. The mutable surface
# ---------------------------------------------------------------------------

class TestMutableSurface:
    """Property 1: reaching outside the surface is a raise, and the deny list is
    the guard's own."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", [
        "api/aria/guard/policy.py",          # the guard itself
        "api/aria/guard/gitguard.py",
        "api/tests/test_improver.py",        # the tests that would catch it
        "api/tests/fixtures/improver_eval.jsonl",   # the evaluator's fixture
        "api/aria/config.py",                # the budgets and caps
        ".env",
        "CLAUDE.md",
        "benchmark-tooling/evalstack/configs/targets.yaml",
        "api/aria/agents/watchdog.py",
        "api/aria/core/killswitch.py",
    ])
    async def test_protected_targets_raise_needs_human(self, path, db):
        with pytest.raises(NeedsHuman) as exc:
            await validate_target(path, db)
        assert exc.value.reason == "protected"
        assert exc.value.severity == "critical"
        # It must name the rule that caught it — an unexplainable refusal is
        # indistinguishable from a bug.
        assert exc.value.pattern

    @pytest.mark.asyncio
    async def test_unprotected_but_unlisted_path_is_still_a_raise(self, db, cfg):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        with pytest.raises(NeedsHuman) as exc:
            await validate_target("api/aria/steward/service.py", db)
        assert exc.value.reason == "off_surface"

    @pytest.mark.asyncio
    async def test_prompt_file_on_the_surface_is_allowed(self, db, cfg):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        target = await validate_target("api/prompts/steward.md", db)
        assert target.kind == KIND_PROMPT_FILE

    @pytest.mark.asyncio
    async def test_it_defers_to_the_guard_and_does_not_keep_its_own_copy(
        self, db, cfg, monkeypatch
    ):
        """Tighten the GUARD's list and the improver's answer must change.

        This is the anti-drift test: a second copy of the deny list inside the
        improver would keep saying yes here, and a permissive drift in a deny
        list is how the tamper check stops meaning anything.
        """
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        assert (await validate_target("api/prompts/steward.md", db)).ref

        monkeypatch.setattr(
            settings, "guard_protected_paths",
            list(settings.guard_protected_paths) + ["api/prompts/**"],
        )
        guard_policy.load_policy(force=True)
        try:
            with pytest.raises(NeedsHuman) as exc:
                await validate_target("api/prompts/steward.md", db)
            assert exc.value.reason == "protected"
        finally:
            monkeypatch.undo()
            guard_policy.load_policy(force=True)

    @pytest.mark.asyncio
    async def test_star_does_not_cross_a_directory_boundary(self, db, cfg):
        """`fnmatch` would let `api/*` match `api/aria/guard/x.py`. The guard's
        matcher does not, and the improver uses the guard's matcher."""
        cfg["improver_mutable_paths"] = ["api/*"]
        with pytest.raises(NeedsHuman):
            await validate_target("api/aria/steward/service.py", db)

    @pytest.mark.asyncio
    async def test_path_escaping_the_repo_is_protected(self, db, cfg):
        cfg["improver_mutable_paths"] = ["**"]
        with pytest.raises(NeedsHuman) as exc:
            await validate_target("../../.ssh/id_ed25519", db)
        assert exc.value.reason == "protected"

    @pytest.mark.asyncio
    async def test_agent_prompt_field_allowlist(self, db):
        ok = await validate_target("db.agents:pi-coding#system_prompt", db)
        assert ok.kind == KIND_AGENT_PROMPT and ok.ref == "pi-coding"
        for field in ("llm.backend", "enabled", "model"):
            with pytest.raises(NeedsHuman) as exc:
                await validate_target(f"db.agents:pi-coding#{field}", db)
            assert exc.value.reason == "off_surface"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", [
        "improver_auto_apply_after_clean_promotions",   # its own leash
        "improver_max_proposals_per_run",
        "guard_diff_max_lines",                          # the merge gate
        "killswitch_enabled",
        "spend_cap_usd_per_hour",
        "coding_gate_enabled",
        "outcome_review_family",                         # who judges it
    ])
    async def test_thresholds_that_bound_the_improver_are_refused(self, key, db, cfg):
        cfg["improver_mutable_thresholds"] = [key]   # even if explicitly listed
        with pytest.raises(NeedsHuman) as exc:
            await validate_target(f"threshold:{key}", db)
        assert exc.value.reason == "protected"

    @pytest.mark.asyncio
    async def test_allowed_threshold_needs_to_be_listed(self, db, cfg):
        cfg["improver_mutable_thresholds"] = []
        with pytest.raises(NeedsHuman) as exc:
            await validate_target("threshold:routing_deep_confidence", db)
        assert exc.value.reason == "off_surface"
        cfg["improver_mutable_thresholds"] = ["routing_deep_confidence"]
        assert (await validate_target("threshold:routing_deep_confidence", db)).kind \
            == KIND_THRESHOLD

    @pytest.mark.asyncio
    async def test_malformed_target_is_not_guessed_at(self, db):
        for bad in ("", None, {"kind": "wat", "ref": ""}):
            with pytest.raises(NeedsHuman):
                await validate_target(bad, db)

    def test_target_parsing_round_trips(self):
        assert parse_target("api/prompts/x.md").kind == KIND_PROMPT_FILE
        assert parse_target("agent:pi#system_prompt").canonical == \
            "db.agents:pi#system_prompt"
        assert parse_target("threshold:k").canonical == "threshold:k"
        assert parse_target("skill:s").kind == KIND_SKILL


# ---------------------------------------------------------------------------
# 2. A proposal off the surface is a RAISE, with evidence
# ---------------------------------------------------------------------------

class TestOffSurfaceIsARaise:

    @pytest.mark.asyncio
    async def test_protected_proposal_alerts_and_records_a_guard_event(
        self, db, repo, worktree, cfg, monkeypatch
    ):
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 1
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        _outcomes(db, 10, 8)
        notifier = Notifier()

        async def proposer(*, baseline):
            return [{
                "target": "api/aria/guard/policy.py",
                "after": "def is_protected(*a, **k): return False\n",
                "rationale": "the gate keeps rejecting my work",
                "metric": "success_rate",
            }]

        worker = Improver(db, notifier, proposer=proposer,
                          git_guard=FakeGuard(str(worktree)), repo_root_path=str(repo))
        result = await worker.run_once()

        assert result["proposals"][0]["status"] == "needs_human"
        # (a) Ben is paged, at critical, and it is not acked-by-default noise.
        alerts = [a for a in notifier.sent if a["needs_human"]]
        assert alerts and alerts[0]["severity"] == "critical"
        assert "guard" in alerts[0]["detail"]
        # (b) the guard's own blocked-event log carries it
        events = db["guard_events"].docs
        assert events and events[0]["blocked"] is True
        assert events[0]["severity"] == "critical"
        # (c) the attempt is preserved as evidence, not swallowed
        versions = db["policy_versions"].docs
        assert versions and versions[0]["status"] == STATUS_REJECTED
        # (d) and nothing was written to the target
        assert not (repo / "api" / "aria").exists()

    @pytest.mark.asyncio
    async def test_off_surface_proposal_raises_at_high_not_critical(
        self, db, repo, worktree, cfg, monkeypatch
    ):
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 1
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        _outcomes(db, 10, 8)
        notifier = Notifier()

        async def proposer(*, baseline):
            return [{"target": "api/aria/steward/service.py", "after": "x = 1\n",
                     "rationale": "tidy up", "metric": "success_rate"}]

        worker = Improver(db, notifier, proposer=proposer,
                          git_guard=FakeGuard(str(worktree)), repo_root_path=str(repo))
        await worker.run_once()
        assert notifier.sent[0]["severity"] == "high"
        assert notifier.sent[0]["needs_human"] is True


# ---------------------------------------------------------------------------
# 3. No baseline, no proposal
# ---------------------------------------------------------------------------

class TestBaselineRefusal:

    @pytest.mark.asyncio
    async def test_no_labelled_outcomes_means_no_proposal_and_no_model_call(
        self, db, repo, cfg, monkeypatch
    ):
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 20
        called = []

        async def proposer(*, baseline):
            called.append(baseline)
            return []

        worker = Improver(db, Notifier(), proposer=proposer, repo_root_path=str(repo))
        result = await worker.run_once()
        assert result["status"] == "insufficient_data"
        assert "need 20" in result["detail"]
        assert called == []          # a model was never asked

    @pytest.mark.asyncio
    async def test_partial_data_still_refuses(self, db, repo, cfg, monkeypatch):
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 20
        _outcomes(db, 19, 15)
        worker = Improver(db, Notifier(), proposer=_boom, repo_root_path=str(repo))
        assert (await worker.run_once())["status"] == "insufficient_data"

    @pytest.mark.asyncio
    async def test_disabled_is_a_no_op(self, db, repo, monkeypatch):
        monkeypatch.setattr(settings, "improver_enabled", False)
        worker = Improver(db, Notifier(), proposer=_boom, repo_root_path=str(repo))
        assert (await worker.run_once())["status"] == "disabled"

    @pytest.mark.asyncio
    async def test_estop_halts_the_tick(self, db, repo, cfg, monkeypatch):
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 1
        _outcomes(db, 10, 9)
        estop = SimpleNamespace(is_active=_true)
        worker = Improver(db, Notifier(), proposer=_boom, estop=estop,
                          repo_root_path=str(repo))
        result = await worker.run_once()
        assert result["status"] == "halted" and result["reason"] == "estop"

    @pytest.mark.asyncio
    async def test_unlabelled_outcomes_are_not_counted_as_failures(self, db):
        db["session_outcomes"].docs.extend([
            {"_id": "a", "label": "success", "created_at": datetime.now(timezone.utc)},
            {"_id": "b", "created_at": datetime.now(timezone.utc)},        # no label
            {"_id": "c", "label": "failed", "created_at": datetime.now(timezone.utc)},
        ])
        baseline = await collect_baseline(db)
        assert baseline.labelled_outcomes == 2
        assert baseline.success_rate == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_missing_outcomes_module_degrades_to_zero(self, db):
        """`steward/outcomes.py` is another work stream's file. Whether or not it
        exists in this checkout, the improver must read a baseline of zero rather
        than fail to import."""
        assert improve.OUTCOMES_COLLECTION == "session_outcomes"
        baseline = await collect_baseline(db)
        assert baseline.labelled_outcomes == 0
        assert baseline.sources["outcomes_collection"] == "session_outcomes"

    @pytest.mark.asyncio
    async def test_baseline_reads_gates_alerts_and_usage(self, db):
        now = datetime.now(timezone.utc)
        _outcomes(db, 4, 3)
        db["guard_gate_runs"].docs.extend([
            {"_id": "g1", "passed": True, "at": now},
            {"_id": "g2", "passed": False, "at": now},
        ])
        db["alerts"].docs.extend([
            {"_id": "a1", "needs_human": True, "created_at": now, "false_raise": True},
            {"_id": "a2", "needs_human": True, "created_at": now},
        ])
        db["usage"].docs.append({
            "_id": "u1", "timestamp": now, "total_tokens": 400,
            "input_tokens": 300, "output_tokens": 100, "model": "qwen", "backend": "llamacpp",
        })
        baseline = await collect_baseline(db)
        assert baseline.gate_pass_rate == pytest.approx(0.5)
        assert baseline.raises == 2 and baseline.false_raises == 1
        assert baseline.false_raise_rate == pytest.approx(0.5)
        assert baseline.avg_tokens == pytest.approx(100.0)   # 400 tokens / 4 outcomes


async def _boom(**kwargs):
    raise AssertionError("the proposer must not be called")


async def _true():
    return True


# ---------------------------------------------------------------------------
# 4. The version store IS the undo
# ---------------------------------------------------------------------------

class TestPolicyVersionStore:

    @pytest.mark.asyncio
    async def test_rollback_restores_before_verbatim(self, store, repo, cfg):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        path = repo / "api" / "prompts" / "steward.md"
        original = path.read_text()
        target = Target(kind=KIND_PROMPT_FILE, ref="api/prompts/steward.md")

        version = await store.propose(target=target, after="AFTER prompt\n",
                                      rationale="r", proposer="test")
        await store.record_gate(version["_id"], {"passed": True})
        await store.promote(version["_id"], actor="ben")
        assert path.read_text() == "AFTER prompt\n"

        rolled = await store.rollback(version["_id"], actor="ben", reason="regression")
        assert path.read_text() == original
        assert rolled["status"] == STATUS_ROLLED_BACK
        # The promoted text is kept: a rollback must not be a second way to lose
        # work.
        assert rolled["rolled_back_from"] == "AFTER prompt\n"

    @pytest.mark.asyncio
    async def test_promote_refuses_without_a_passing_gate(self, store, cfg):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        version = await store.propose(
            target=Target(kind=KIND_PROMPT_FILE, ref="api/prompts/steward.md"),
            after="X\n", rationale="r", proposer="test")
        with pytest.raises(ImproverError, match="no passing gate"):
            await store.promote(version["_id"], actor="ben")

    @pytest.mark.asyncio
    async def test_promote_refuses_on_drift(self, store, repo, cfg):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        path = repo / "api" / "prompts" / "steward.md"
        version = await store.propose(
            target=Target(kind=KIND_PROMPT_FILE, ref="api/prompts/steward.md"),
            after="AFTER\n", rationale="r", proposer="test")
        await store.record_gate(version["_id"], {"passed": True})
        path.write_text("Ben edited this by hand\n")
        with pytest.raises(ImproverError, match="drift"):
            await store.promote(version["_id"], actor="ben")
        assert path.read_text() == "Ben edited this by hand\n"

    @pytest.mark.asyncio
    async def test_promote_revalidates_the_target_at_apply_time(
        self, store, repo, cfg, monkeypatch
    ):
        """The surface can shrink between proposal and APPLY, and the answer that
        governs the write is the current one."""
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        version = await store.propose(
            target=Target(kind=KIND_PROMPT_FILE, ref="api/prompts/steward.md"),
            after="AFTER\n", rationale="r", proposer="test")
        await store.record_gate(version["_id"], {"passed": True})
        cfg["improver_mutable_paths"] = []          # surface withdrawn
        with pytest.raises(NeedsHuman):
            await store.promote(version["_id"], actor="ben")
        assert (repo / "api" / "prompts" / "steward.md").read_text() == "BEFORE prompt\n"

    @pytest.mark.asyncio
    async def test_empty_candidate_is_never_written(self, store, repo, cfg):
        """Qwen3.8 emits reasoning_content before content, so a tight budget
        returns empty content with finish_reason=length. Writing it would blank a
        prompt file — the DS4 zero-entity failure with a bigger blast radius."""
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        target = Target(kind=KIND_PROMPT_FILE, ref="api/prompts/steward.md")
        with pytest.raises(ImproverError, match="empty"):
            await store._apply(target, "")
        with pytest.raises(ImproverError, match="empty"):
            await store._apply(target, None)
        assert (repo / "api" / "prompts" / "steward.md").read_text() == "BEFORE prompt\n"

    @pytest.mark.asyncio
    async def test_agent_prompt_target_writes_only_that_field(self, store, db):
        db["agents"].docs.append({"_id": "1", "slug": "pi-coding",
                                  "system_prompt": "old", "enabled": True,
                                  "llm": {"backend": "llamacpp"}})
        target = Target(kind=KIND_AGENT_PROMPT, ref="pi-coding", field="system_prompt")
        version = await store.propose(target=target, after="new", rationale="r",
                                      proposer="test")
        assert version["before"] == "old"
        await store.record_gate(version["_id"], {"passed": True})
        await store.promote(version["_id"], actor="ben")
        doc = db["agents"].docs[0]
        assert doc["system_prompt"] == "new"
        assert doc["enabled"] is True and doc["llm"] == {"backend": "llamacpp"}
        await store.rollback(version["_id"], actor="ben")
        assert db["agents"].docs[0]["system_prompt"] == "old"

    @pytest.mark.asyncio
    async def test_threshold_lands_in_the_override_collection_not_config(
        self, store, db, cfg
    ):
        """config.py is protected, so a promoted threshold is data. If it wrote
        config.py the improver would be editing the file that bounds it."""
        cfg["improver_mutable_thresholds"] = ["routing_deep_confidence"]
        target = Target(kind=KIND_THRESHOLD, ref="routing_deep_confidence")
        version = await store.propose(target=target, after="0.72", rationale="r",
                                      proposer="test")
        await store.record_gate(version["_id"], {"passed": True})
        await store.promote(version["_id"], actor="ben")
        row = db["policy_overrides"].docs[0]
        assert row["_id"] == "routing_deep_confidence" and row["value"] == 0.72
        await store.rollback(version["_id"], actor="ben")
        assert db["policy_overrides"].docs == []      # there was no `before`

    @pytest.mark.asyncio
    async def test_only_promoted_versions_can_be_rolled_back(self, store, cfg):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        version = await store.propose(
            target=Target(kind=KIND_PROMPT_FILE, ref="api/prompts/steward.md"),
            after="A\n", rationale="r", proposer="test")
        with pytest.raises(ImproverError, match="only a"):
            await store.rollback(version["_id"])

    @pytest.mark.asyncio
    async def test_clean_promotions_counts_only_survivors(self, store, db, cfg):
        db["policy_versions"].docs.extend([
            {"_id": "1", "target_kind": KIND_PROMPT_FILE, "status": STATUS_PROMOTED,
             "watch": {"clean": True}},
            {"_id": "2", "target_kind": KIND_PROMPT_FILE, "status": STATUS_PROMOTED,
             "watch": {"clean": False}},
            {"_id": "3", "target_kind": KIND_PROMPT_FILE, "status": STATUS_ROLLED_BACK,
             "watch": {"clean": True}},
        ])
        assert await store.clean_promotions(KIND_PROMPT_FILE) == 1


# ---------------------------------------------------------------------------
# 5. The gate
# ---------------------------------------------------------------------------

def _judge(verdict="promote", family="claude", destructive=False):
    async def judge(*, version, target, baseline, candidate):
        return {"verdict": verdict, "family": family, "destructive": destructive,
                "reasons": "ok"}
    return judge


class _Runner:
    """Deterministic stand-in for a model replaying a fixture case: the output is
    the policy text, so a candidate that adds the required token scores higher."""

    def __init__(self, cost=0.001, tokens=100, empty=False):
        self.cost, self.tokens, self.empty = cost, tokens, empty

    async def __call__(self, policy_text, case):
        return {"output": "" if self.empty else f"{policy_text}|{case.get('id')}",
                "tokens": self.tokens, "cost_usd": self.cost}


async def _run_cmd_ok(argv, cwd, timeout):
    if argv[:2] == ["git", "status"]:
        return 0, " M api/prompts/steward.md\n"
    return 0, "42 passed"


async def _run_cmd_pytest_fail(argv, cwd, timeout):
    if argv[:2] == ["git", "status"]:
        return 0, " M api/prompts/steward.md\n"
    return 1, "E   assert False\n1 failed, 41 passed"


def _version(after="AFTER needle\n", before="BEFORE\n", proposer="llamacpp:qwen3.8"):
    return {"_id": "pv-test", "target": "api/prompts/steward.md",
            "target_kind": KIND_PROMPT_FILE, "target_ref": "api/prompts/steward.md",
            "target_field": None, "before": before, "after": after,
            "rationale": "raise nudge recovery", "proposer": proposer}


class TestGate:

    def _improver(self, db, repo, worktree, cfg, **kw):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        cases = [{"id": "c1", "checks": [{"kind": "contains", "value": "needle"}]},
                 {"id": "c2", "checks": [{"kind": "contains", "value": "needle"}]}]
        evaluator = FixtureEvaluator(kw.pop("runner", _Runner()), cases=cases)
        return Improver(db, Notifier(), git_guard=kw.pop("guard", FakeGuard(str(worktree))),
                        evaluator=evaluator, judge=kw.pop("judge", _judge()),
                        run_cmd=kw.pop("run_cmd", _run_cmd_ok),
                        repo_root_path=str(repo), **kw)

    @pytest.mark.asyncio
    async def test_green_gate_carries_its_evidence(self, db, repo, worktree, cfg):
        worker = self._improver(db, repo, worktree, cfg)
        gate = await worker.gate(_version())
        assert gate["passed"] is True
        names = {c["name"] for c in gate["checks"]}
        assert {"destructive_scan", "worktree", "single_target", "pytest",
                "success_not_worse", "cost_not_worse",
                "different_family_review"} <= names
        assert gate["candidate"]["success"] == 1.0
        assert gate["baseline"]["success"] == 0.0
        # The candidate was evaluated in the WORKTREE. The live checkout is
        # untouched even by a gate that passes — promotion is a separate,
        # decided act (§4.3: at A ≤ 2 a merge is Ben's `APPLY <id>`).
        assert (worktree / "api" / "prompts" / "steward.md").read_text() == \
            "AFTER needle\n"
        assert (repo / "api" / "prompts" / "steward.md").read_text() == "BEFORE prompt\n"

    @pytest.mark.asyncio
    async def test_worktree_is_always_discarded(self, db, repo, worktree, cfg):
        guard = FakeGuard(str(worktree))
        worker = self._improver(db, repo, worktree, cfg, guard=guard,
                                run_cmd=_run_cmd_pytest_fail)
        await worker.gate(_version())
        assert guard.prepared == guard.discarded != []

    @pytest.mark.asyncio
    async def test_a_raising_gate_still_discards_and_never_passes(self, db, repo, worktree, cfg):
        guard = FakeGuard(str(worktree))

        async def explode(argv, cwd, timeout):
            raise RuntimeError("subprocess exploded")

        worker = self._improver(db, repo, worktree, cfg, guard=guard, run_cmd=explode)
        gate = await worker.gate(_version())
        assert gate["passed"] is False
        assert guard.discarded == guard.prepared != []

    @pytest.mark.asyncio
    async def test_no_guard_means_no_evaluation_in_the_live_checkout(self, db, repo, worktree, cfg):
        worker = self._improver(db, repo, worktree, cfg, guard=None)
        worker._guard = None
        gate = await worker.gate(_version())
        assert gate["passed"] is False
        assert "worktree" in gate["summary"]
        # the live file is untouched
        assert (repo / "api" / "prompts" / "steward.md").read_text() == "BEFORE prompt\n"

    @pytest.mark.asyncio
    async def test_pytest_failure_rejects_with_evidence(self, db, repo, worktree, cfg):
        worker = self._improver(db, repo, worktree, cfg, run_cmd=_run_cmd_pytest_fail)
        gate = await worker.gate(_version())
        assert gate["passed"] is False
        pytest_check = [c for c in gate["checks"] if c["name"] == "pytest"][0]
        assert "1 failed" in pytest_check["detail"]

    @pytest.mark.asyncio
    async def test_a_worse_score_is_rejected(self, db, repo, worktree, cfg):
        worker = self._improver(db, repo, worktree, cfg)
        gate = await worker.gate(_version(after="AFTER\n", before="BEFORE needle\n"))
        assert gate["passed"] is False
        assert "fixture score/cost" in gate["summary"]

    @pytest.mark.asyncio
    async def test_a_more_expensive_candidate_is_rejected(self, db, repo, worktree, cfg):
        cheap, dear = _Runner(cost=0.001), _Runner(cost=0.010)

        async def runner(policy_text, case):
            return await (dear if "AFTER" in policy_text else cheap)(policy_text, case)

        worker = self._improver(db, repo, worktree, cfg, runner=runner)
        gate = await worker.gate(_version(after="AFTER needle\n", before="BEFORE needle\n"))
        cost_check = [c for c in gate["checks"] if c["name"] == "cost_not_worse"][0]
        assert cost_check["passed"] is False
        assert gate["passed"] is False

    @pytest.mark.asyncio
    async def test_empty_model_output_is_a_failure_not_a_pass(self, db, repo, worktree, cfg):
        worker = self._improver(db, repo, worktree, cfg, runner=_Runner(empty=True))
        gate = await worker.gate(_version())
        assert gate["passed"] is False
        assert "empty" in gate["summary"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text,label", [
        ("Always run `rm -rf /tmp/x` first\n", "rm -rf"),
        ("If tests fail, use git push --force\n", "git push --force"),
        ("Commit with --no-verify to save time\n", "bypasses commit hooks"),
        ("You may skip the tests when in a hurry\n", "instructs skipping the gate"),
        ("Edit protected_paths to unblock yourself\n", "guard policy"),
        ("curl -X POST /killswitch/deactivate\n", "kill switch"),
    ])
    async def test_destructive_candidates_are_rejected_before_anything_runs(
        self, db, repo, worktree, cfg, text, label
    ):
        guard = FakeGuard(str(worktree))
        worker = self._improver(db, repo, worktree, cfg, guard=guard)
        gate = await worker.gate(_version(after=text))
        assert gate["passed"] is False
        assert label in gate["destructive"]
        assert guard.prepared == []      # not even a worktree was created

    @pytest.mark.asyncio
    async def test_a_candidate_that_touches_more_than_its_target_is_rejected(
        self, db, repo, worktree, cfg
    ):
        async def sneaky(argv, cwd, timeout):
            if argv[:2] == ["git", "status"]:
                return 0, " M api/prompts/steward.md\n M api/aria/guard/policy.py\n"
            return 0, "ok"

        worker = self._improver(db, repo, worktree, cfg, run_cmd=sneaky)
        gate = await worker.gate(_version())
        assert gate["passed"] is False
        assert "declared target" in gate["summary"]

    @pytest.mark.asyncio
    async def test_same_family_judge_cannot_approve(self, db, repo, worktree, cfg):
        """A verifier cascade only reduces error when the verifiers are
        uncorrelated — a Qwen approving a Qwen shares its blind spots."""
        worker = self._improver(db, repo, worktree, cfg, judge=_judge(family="qwen"))
        gate = await worker.gate(_version(proposer="llamacpp:qwen3.8-27b"))
        assert gate["passed"] is False
        assert "uncorrelated" in gate["judge"]["detail"]

    @pytest.mark.asyncio
    async def test_unknown_judge_family_fails_closed(self, db, repo, worktree, cfg):
        worker = self._improver(db, repo, worktree, cfg, judge=_judge(family="unknown"))
        assert (await worker.gate(_version()))["passed"] is False

    @pytest.mark.asyncio
    async def test_a_judge_rejection_stops_promotion(self, db, repo, worktree, cfg):
        worker = self._improver(db, repo, worktree, cfg, judge=_judge(verdict="reject"))
        gate = await worker.gate(_version())
        assert gate["passed"] is False
        assert "review rejected" in gate["summary"]

    @pytest.mark.asyncio
    async def test_a_judge_that_errors_does_not_pass_the_gate(self, db, repo, worktree, cfg):
        async def broken(**kwargs):
            raise RuntimeError("no API key")

        worker = self._improver(db, repo, worktree, cfg, judge=broken)
        gate = await worker.gate(_version())
        assert gate["passed"] is False
        assert "judge unavailable" in gate["judge"]["detail"]

    @pytest.mark.asyncio
    async def test_no_evaluator_means_no_promotion(self, db, repo, worktree, cfg):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        worker = Improver(db, Notifier(), git_guard=FakeGuard(str(worktree)),
                          judge=_judge(), run_cmd=_run_cmd_ok,
                          repo_root_path=str(repo))
        gate = await worker.gate(_version())
        assert gate["passed"] is False
        assert "no evaluator" in gate["reasons"]

    @pytest.mark.asyncio
    async def test_evalstack_gate_refuses_to_pick_its_own_target(self, db, repo, worktree, cfg):
        cfg["improver_evalstack_enabled"] = True
        cfg["improver_eval_targets"] = []
        worker = self._improver(db, repo, worktree, cfg, benchmarks=SimpleNamespace())
        gate = await worker.gate(_version())
        assert gate["passed"] is False
        suite = [c for c in gate["checks"] if c["name"] == "evalstack"][0]
        assert "refusing to pick" in suite["detail"]


# ---------------------------------------------------------------------------
# 6. The frozen fixture
# ---------------------------------------------------------------------------

class TestFrozenFixture:

    def test_fixture_must_be_inside_the_guards_protected_paths(self, repo, cfg):
        import json

        loose = repo / "api" / "prompts" / "eval.jsonl"
        loose.write_text(json.dumps({"id": "c", "checks": []}))
        cfg["improver_eval_fixture"] = "api/prompts/eval.jsonl"
        with pytest.raises(ImproverError, match="guard_protected_paths"):
            load_fixture()

    def test_fixture_outside_the_repo_is_refused(self, repo, cfg, tmp_path):
        import json

        outside = tmp_path.parent / "loose_eval.jsonl"
        outside.write_text(json.dumps({"id": "c", "checks": []}))
        cfg["improver_eval_fixture"] = str(outside)
        with pytest.raises(ImproverError, match="inside the repo"):
            load_fixture()

    def test_protected_fixture_loads(self, repo, cfg):
        _fixture_file(repo, cfg, [{"id": "c1", "checks": [
            {"kind": "contains", "value": "x"}]}])
        assert len(load_fixture()) == 1

    def test_an_empty_fixture_is_an_error_not_a_perfect_score(self, repo, cfg):
        _fixture_file(repo, cfg, [])
        with pytest.raises(ImproverError, match="no cases"):
            load_fixture()

    def test_checks_are_deterministic_predicates(self):
        case = {"checks": [
            {"kind": "contains", "value": "alpha"},
            {"kind": "not_contains", "value": "beta"},
            {"kind": "regex", "value": r"al\w+"},
            {"kind": "max_chars", "value": 100},
            {"kind": "json_has", "value": "k"},
        ]}
        passed, total, failures = score_case(case, "alpha")
        assert total == 5 and passed == 4 and failures == ["json_has:k"]

    @pytest.mark.asyncio
    async def test_an_erroring_case_counts_against_the_candidate(self):
        async def runner(policy_text, case):
            raise RuntimeError("model down")

        evaluator = FixtureEvaluator(runner, cases=[
            {"id": "c1", "checks": [{"kind": "contains", "value": "x"}]}])
        result = await evaluator.evaluate("policy")
        assert result.errors == 1 and result.success == 0.0


# ---------------------------------------------------------------------------
# 7. Auto-apply is earned
# ---------------------------------------------------------------------------

class TestAutoApply:

    def _worker(self, db, repo, worktree, cfg, **kw):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        cases = [{"id": "c1", "checks": [{"kind": "contains", "value": "needle"}]}]
        return Improver(db, kw.pop("notifier", Notifier()),
                        git_guard=FakeGuard(str(worktree)),
                        evaluator=FixtureEvaluator(_Runner(), cases=cases),
                        judge=_judge(), run_cmd=_run_cmd_ok,
                        repo_root_path=str(repo), **kw)

    @pytest.mark.asyncio
    async def test_below_the_threshold_it_asks_ben(self, db, repo, worktree, cfg, monkeypatch):
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 1
        cfg["improver_auto_apply_after_clean_promotions"] = 10
        _outcomes(db, 10, 8)
        notifier = Notifier()

        async def proposer(*, baseline):
            return [{"target": "api/prompts/steward.md", "after": "AFTER needle\n",
                     "rationale": "lift nudge recovery", "metric": "success_rate"}]

        worker = self._worker(db, repo, worktree, cfg, notifier=notifier, proposer=proposer)
        result = await worker.run_once()

        assert result["proposals"][0]["status"] == "awaiting_apply"
        alert = notifier.of_kind("proposal")[0]
        assert alert["needs_human"] is True
        assert "APPLY" in alert["detail"]
        assert alert["proposal"]["action"] == "APPLY"
        # nothing applied
        assert (repo / "api" / "prompts" / "steward.md").read_text() == "BEFORE prompt\n"
        assert db["policy_versions"].docs[0]["status"] == STATUS_PROPOSED

    @pytest.mark.asyncio
    async def test_earned_auto_apply_writes_and_arms_a_watch(
        self, db, repo, worktree, cfg, monkeypatch
    ):
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 1
        cfg["improver_auto_apply_after_clean_promotions"] = 2
        _outcomes(db, 10, 8)
        for i in range(2):      # two promotions that survived their watch
            db["policy_versions"].docs.append({
                "_id": f"old{i}", "target_kind": KIND_PROMPT_FILE,
                "status": STATUS_PROMOTED, "watch": {"clean": True}})
        notifier = Notifier()

        async def proposer(*, baseline):
            return [{"target": "api/prompts/steward.md", "after": "AFTER needle\n",
                     "rationale": "lift nudge recovery", "metric": "success_rate"}]

        worker = self._worker(db, repo, worktree, cfg, notifier=notifier, proposer=proposer)
        result = await worker.run_once()

        assert result["proposals"][0]["status"] == STATUS_PROMOTED
        assert (repo / "api" / "prompts" / "steward.md").read_text() == "AFTER needle\n"
        promoted = [d for d in db["policy_versions"].docs if d["_id"].startswith("pv-")][0]
        assert promoted["auto_applied"] is True
        assert promoted["watch"]["active"] is True
        # It is announced, but as information — an auto-apply is not a question.
        assert notifier.of_kind("auto_promoted")[0]["needs_human"] is False

    @pytest.mark.asyncio
    async def test_skills_and_heuristics_never_earn_auto_apply(self, db, repo, worktree, cfg):
        cfg["improver_auto_apply_after_clean_promotions"] = 1
        for kind in ("skill", "heuristic"):
            for i in range(50):
                db["policy_versions"].docs.append({
                    "_id": f"{kind}{i}", "target_kind": kind,
                    "status": STATUS_PROMOTED, "watch": {"clean": True}})
        worker = self._worker(db, repo, worktree, cfg)
        assert await worker.auto_apply_allowed("skill") is False
        assert await worker.auto_apply_allowed("heuristic") is False
        assert await worker.auto_apply_allowed(KIND_PROMPT_FILE) is False

    @pytest.mark.asyncio
    async def test_zero_disables_auto_apply_entirely(self, db, repo, worktree, cfg):
        cfg["improver_auto_apply_after_clean_promotions"] = 0
        db["policy_versions"].docs.append({
            "_id": "x", "target_kind": KIND_PROMPT_FILE, "status": STATUS_PROMOTED,
            "watch": {"clean": True}})
        worker = self._worker(db, repo, worktree, cfg)
        assert await worker.auto_apply_allowed(KIND_PROMPT_FILE) is False

    @pytest.mark.asyncio
    async def test_a_never_executed_skill_is_not_curated(self, db, repo, worktree, cfg, monkeypatch):
        """Voyager's rule: a skill enters the library after it has run."""
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 1
        _outcomes(db, 10, 9)
        db["skills"].docs.append({"_id": "s", "name": "deploy", "content": "old"})

        async def proposer(*, baseline):
            return [{"target": "skill:deploy", "after": "new body",
                     "rationale": "tidier", "metric": "success_rate"}]

        worker = self._worker(db, repo, worktree, cfg, proposer=proposer)
        result = await worker.run_once()
        assert result["proposals"][0]["reason"] == "skill never executed"
        assert db["skills"].docs[0]["content"] == "old"

    @pytest.mark.asyncio
    async def test_max_proposals_per_run_is_honoured(self, db, repo, worktree, cfg, monkeypatch):
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 1
        cfg["improver_max_proposals_per_run"] = 1
        _outcomes(db, 10, 8)

        async def proposer(*, baseline):
            return [
                {"target": "api/prompts/steward.md", "after": "AFTER needle A\n",
                 "rationale": "a", "metric": "success_rate"},
                {"target": "api/prompts/steward.md", "after": "AFTER needle B\n",
                 "rationale": "b", "metric": "success_rate"},
            ]

        worker = self._worker(db, repo, worktree, cfg, proposer=proposer)
        result = await worker.run_once()
        assert len(result["proposals"]) == 1

    @pytest.mark.asyncio
    async def test_a_proposal_without_a_rationale_is_discarded(
        self, db, repo, worktree, cfg, monkeypatch
    ):
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 1
        _outcomes(db, 10, 8)

        async def proposer(*, baseline):
            return [{"target": "api/prompts/steward.md", "after": "AFTER needle\n",
                     "rationale": "  "}]

        worker = self._worker(db, repo, worktree, cfg, proposer=proposer)
        result = await worker.run_once()
        assert result["proposals"][0]["status"] == "discarded"
        assert db["policy_versions"].docs == []


# ---------------------------------------------------------------------------
# 8. The regression watch
# ---------------------------------------------------------------------------

class TestRegressionWatch:

    def _promoted(self, db, *, baseline_rate=0.8, hours_ago=1, until_hours=72):
        now = datetime.now(timezone.utc)
        db["policy_versions"].docs.append({
            "_id": "pv-1", "id": "pv-1", "target": "api/prompts/steward.md",
            "target_kind": KIND_PROMPT_FILE, "target_ref": "api/prompts/steward.md",
            "target_field": None, "before": "BEFORE prompt\n", "after": "AFTER\n",
            "status": STATUS_PROMOTED, "promoted_at": now - timedelta(hours=hours_ago),
            "baseline_metrics": {"success_rate": baseline_rate},
            "watch": {"active": True, "clean": False,
                      "until": now + timedelta(hours=until_hours),
                      "metric": "success_rate"},
        })

    @pytest.mark.asyncio
    async def test_a_significant_regression_rolls_back_and_raises(self, db, repo, cfg):
        cfg["improver_regression_min_outcomes"] = 5
        cfg["improver_regression_tolerance"] = 0.05
        (repo / "api" / "prompts" / "steward.md").write_text("AFTER\n")
        self._promoted(db, baseline_rate=0.9)
        _outcomes(db, 10, 3, when=datetime.now(timezone.utc))   # 0.3 vs 0.9

        notifier = Notifier()
        worker = Improver(db, notifier, repo_root_path=str(repo))
        out = await worker.check_regressions()

        assert out[0]["rolled_back"] is True
        assert db["policy_versions"].docs[0]["status"] == STATUS_ROLLED_BACK
        assert (repo / "api" / "prompts" / "steward.md").read_text() == "BEFORE prompt\n"
        alert = notifier.of_kind("auto_rollback")[0]
        assert alert["needs_human"] is True and alert["severity"] == "high"

    @pytest.mark.asyncio
    async def test_too_few_outcomes_does_not_roll_back(self, db, repo, cfg):
        cfg["improver_regression_min_outcomes"] = 10
        self._promoted(db, baseline_rate=0.9)
        _outcomes(db, 3, 0, when=datetime.now(timezone.utc))
        worker = Improver(db, Notifier(), repo_root_path=str(repo))
        assert await worker.check_regressions() == []
        assert db["policy_versions"].docs[0]["status"] == STATUS_PROMOTED

    @pytest.mark.asyncio
    async def test_noise_inside_the_tolerance_does_not_roll_back(self, db, repo, cfg):
        cfg["improver_regression_min_outcomes"] = 5
        cfg["improver_regression_tolerance"] = 0.15
        self._promoted(db, baseline_rate=0.8)
        _outcomes(db, 10, 7, when=datetime.now(timezone.utc))   # 0.7 vs 0.8
        worker = Improver(db, Notifier(), repo_root_path=str(repo))
        assert await worker.check_regressions() == []

    @pytest.mark.asyncio
    async def test_surviving_the_window_with_data_makes_it_clean(self, db, repo, cfg):
        cfg["improver_regression_min_outcomes"] = 5
        self._promoted(db, baseline_rate=0.5, until_hours=-1)   # window elapsed
        _outcomes(db, 10, 9, when=datetime.now(timezone.utc))
        worker = Improver(db, Notifier(), repo_root_path=str(repo))
        out = await worker.check_regressions()
        assert out[0]["clean"] is True
        watch = db["policy_versions"].docs[0]["watch"]
        assert watch["active"] is False and watch["clean"] is True

    @pytest.mark.asyncio
    async def test_an_unmeasured_window_is_not_a_clean_promotion(self, db, repo, cfg):
        """Silence must not earn autonomy: no data in the window closes the
        watch, but it does NOT count toward auto-apply."""
        cfg["improver_regression_min_outcomes"] = 5
        self._promoted(db, baseline_rate=0.5, until_hours=-1)
        worker = Improver(db, Notifier(), repo_root_path=str(repo))
        out = await worker.check_regressions()
        assert out[0]["clean"] is False
        assert db["policy_versions"].docs[0]["watch"]["clean"] is False
        assert await worker.store.clean_promotions(KIND_PROMPT_FILE) == 0

    @pytest.mark.asyncio
    async def test_regressions_are_checked_before_new_proposals(
        self, db, repo, cfg, monkeypatch
    ):
        """Stacking a second change on a regressing first one is how a system
        walks away from a working state one improvement at a time."""
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 1
        cfg["improver_regression_min_outcomes"] = 5
        cfg["improver_regression_tolerance"] = 0.05
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        (repo / "api" / "prompts" / "steward.md").write_text("AFTER\n")
        self._promoted(db, baseline_rate=0.9)
        _outcomes(db, 10, 2, when=datetime.now(timezone.utc))

        order: list[str] = []

        async def proposer(*, baseline):
            order.append("propose")
            return []

        worker = Improver(db, Notifier(), proposer=proposer, repo_root_path=str(repo))
        result = await worker.run_once()
        assert result["rollbacks"][0]["rolled_back"] is True
        assert order == ["propose"]           # ran, but after the rollback
        assert db["policy_versions"].docs[0]["status"] == STATUS_ROLLED_BACK


# ---------------------------------------------------------------------------
# 9. Helpers whose failure modes are incident-derived
# ---------------------------------------------------------------------------

class TestHelpers:

    @pytest.mark.parametrize("model,backend,family", [
        ("claude-sonnet-4-5", "anthropic", "claude"),
        ("qwen3.8-27b-rocmfp4-r9700", "llamacpp", "qwen"),
        ("DS4-0731-UD-IQ3-XXS-Halo-DSpark", "llamacpp", "deepseek"),
        ("gemma-4-e4b-it", "llamacpp", "gemma"),
        ("mystery-13b", "custom", "unknown"),
    ])
    def test_model_family(self, model, backend, family):
        assert model_family(model, backend) == family

    def test_extract_json_survives_reasoning_preamble_and_fences(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('Thinking about it...\n{"verdict": "promote"}') == \
            {"verdict": "promote"}
        assert extract_json("no json here") is None
        assert extract_json("") is None

    def test_scan_destructive_is_case_insensitive(self):
        assert "rm -rf" in scan_destructive("RM -RF /home")
        assert scan_destructive("a perfectly ordinary prompt") == []

    @pytest.mark.asyncio
    async def test_background_work_never_goes_to_ds4s_slot(self, db, repo):
        """DS4 on :8108 is pi's single slot; a background call evicts its warm
        prefix (4.2 s warm vs 39.5 s cold)."""
        worker = Improver(db, Notifier(), repo_root_path=str(repo))
        with pytest.raises(ImproverError, match="8108"):
            await worker._complete("llamacpp", "ds4", "hi",
                                   base_url="http://127.0.0.1:8108/v1")

    @pytest.mark.asyncio
    async def test_status_reports_the_surface_and_the_fixture(self, db, repo, cfg):
        _fixture_file(repo, cfg, [{"id": "c", "checks": []}])
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        worker = Improver(db, Notifier(), repo_root_path=str(repo))
        status = await worker.status()
        assert status["mutable_paths"] == ["api/prompts/*.md"]
        assert status["eval_fixture_protected"] is True
        assert status["target_classes"]["skill"]["auto_apply"] is False


# ---------------------------------------------------------------------------
# 10. Routes
# ---------------------------------------------------------------------------

def _app(db, worker=None):
    """A standalone app carrying only this router.

    Deliberately not `aria.main:app`: the improver router is not registered
    there yet (see the INTEGRATION SPEC), and a route test that needs a wiring
    change to run is a route test that will be deleted rather than fixed.
    """
    from fastapi import FastAPI

    from aria.api import deps
    from aria.api.routes import improve as improve_routes

    app = FastAPI()
    app.include_router(improve_routes.router, prefix="/api/v1")
    app.dependency_overrides[deps.get_db] = lambda: db
    if worker is not None:
        app.state.improver = worker
    return app


class TestRoutes:

    @pytest.mark.asyncio
    async def test_reads_are_open_and_carry_the_evidence(self, db, repo, cfg):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        store = PolicyVersionStore(db, str(repo))
        version = await store.propose(
            target=Target(kind=KIND_PROMPT_FILE, ref="api/prompts/steward.md"),
            after="AFTER\n", rationale="because the nudge recovery rate is 0.4",
            proposer="llamacpp:qwen", metric="success_rate")
        await store.record_gate(version["_id"], {
            "passed": True, "summary": "green",
            "candidate": {"success": 0.9}, "baseline": {"success": 0.7},
            "checks": [{"name": "pytest", "passed": True, "detail": "x" * 5000}]})

        app = _app(db)
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            listing = await ac.get("/api/v1/improve/proposals")
            detail = await ac.get(f"/api/v1/improve/proposals/{version['_id']}")
            missing = await ac.get("/api/v1/improve/proposals/nope")
            baseline = await ac.get("/api/v1/improve/baseline")
            status = await ac.get("/api/v1/improve/status")

        assert listing.status_code == 200
        row = listing.json()["proposals"][0]
        assert row["status"] == STATUS_PROPOSED and row["gate_passed"] is True
        assert "before" not in row and row["after_chars"] == len("AFTER\n")
        assert detail.json()["after"] == "AFTER\n"
        assert detail.json()["gate"]["checks"][0]["name"] == "pytest"
        assert missing.status_code == 404
        assert baseline.json()["labelled_outcomes"] == 0
        assert status.json()["enabled"] is False

    @pytest.mark.asyncio
    async def test_promote_requires_the_admin_key(self, db, repo, cfg, monkeypatch):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        monkeypatch.setattr(settings, "admin_key", "s3cret")
        store = PolicyVersionStore(db, str(repo))
        version = await store.propose(
            target=Target(kind=KIND_PROMPT_FILE, ref="api/prompts/steward.md"),
            after="AFTER\n", rationale="r", proposer="test")
        await store.record_gate(version["_id"], {"passed": True})

        app = _app(db)
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            denied = await ac.post(f"/api/v1/improve/proposals/{version['_id']}/promote",
                                   json={})
            assert denied.status_code == 403
            assert (repo / "api" / "prompts" / "steward.md").read_text() == "BEFORE prompt\n"

            ok = await ac.post(f"/api/v1/improve/proposals/{version['_id']}/promote",
                               json={"by": "ben"}, headers={"X-Admin-Key": "s3cret"})
        assert ok.status_code == 200
        assert ok.json()["proposal"]["status"] == STATUS_PROMOTED
        assert (repo / "api" / "prompts" / "steward.md").read_text() == "AFTER\n"

    @pytest.mark.asyncio
    async def test_an_unset_admin_key_fails_closed(self, db, repo, cfg, monkeypatch):
        monkeypatch.setattr(settings, "admin_key", "")
        app = _app(db)
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            resp = await ac.post("/api/v1/improve/proposals/x/promote", json={})
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_promote_without_a_gate_is_refused_even_with_the_admin_key(
        self, db, repo, cfg, monkeypatch
    ):
        """No evaluator, no promotion — there is no override flag, on purpose."""
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        monkeypatch.setattr(settings, "admin_key", "s3cret")
        store = PolicyVersionStore(db, str(repo))
        version = await store.propose(
            target=Target(kind=KIND_PROMPT_FILE, ref="api/prompts/steward.md"),
            after="AFTER\n", rationale="r", proposer="test")
        app = _app(db)
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            resp = await ac.post(f"/api/v1/improve/proposals/{version['_id']}/promote",
                                 json={}, headers={"X-Admin-Key": "s3cret"})
        assert resp.status_code == 409
        assert "gate" in resp.json()["detail"]
        assert (repo / "api" / "prompts" / "steward.md").read_text() == "BEFORE prompt\n"

    @pytest.mark.asyncio
    async def test_reject_is_open_and_writes_nothing_to_the_target(
        self, db, repo, cfg
    ):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        store = PolicyVersionStore(db, str(repo))
        version = await store.propose(
            target=Target(kind=KIND_PROMPT_FILE, ref="api/prompts/steward.md"),
            after="AFTER\n", rationale="r", proposer="test")
        app = _app(db)
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            resp = await ac.post(f"/api/v1/improve/proposals/{version['_id']}/reject",
                                 json={"reason": "not now"})
        assert resp.status_code == 200
        assert resp.json()["proposal"]["status"] == STATUS_REJECTED
        assert (repo / "api" / "prompts" / "steward.md").read_text() == "BEFORE prompt\n"

    @pytest.mark.asyncio
    async def test_rollback_route_restores_and_needs_admin(
        self, db, repo, cfg, monkeypatch
    ):
        cfg["improver_mutable_paths"] = ["api/prompts/*.md"]
        monkeypatch.setattr(settings, "admin_key", "s3cret")
        store = PolicyVersionStore(db, str(repo))
        version = await store.propose(
            target=Target(kind=KIND_PROMPT_FILE, ref="api/prompts/steward.md"),
            after="AFTER\n", rationale="r", proposer="test")
        await store.record_gate(version["_id"], {"passed": True})
        await store.promote(version["_id"], actor="ben")

        app = _app(db)
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            denied = await ac.post(f"/api/v1/improve/proposals/{version['_id']}/rollback",
                                   json={})
            assert denied.status_code == 403
            assert (repo / "api" / "prompts" / "steward.md").read_text() == "AFTER\n"

            ok = await ac.post(f"/api/v1/improve/proposals/{version['_id']}/rollback",
                               json={"reason": "felt wrong"},
                               headers={"X-Admin-Key": "s3cret"})
        assert ok.status_code == 200
        assert (repo / "api" / "prompts" / "steward.md").read_text() == "BEFORE prompt\n"

    @pytest.mark.asyncio
    async def test_run_needs_a_worker(self, db, repo, monkeypatch):
        monkeypatch.setattr(settings, "admin_key", "s3cret")
        app = _app(db)
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            resp = await ac.post("/api/v1/improve/run", headers={"X-Admin-Key": "s3cret"})
        assert resp.status_code == 503

        worker = Improver(db, Notifier(), repo_root_path=str(repo))
        app2 = _app(db, worker=worker)
        async with AsyncClient(transport=ASGITransport(app=app2),
                               base_url="http://test") as ac:
            resp = await ac.post("/api/v1/improve/run", headers={"X-Admin-Key": "s3cret"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"

    @pytest.mark.asyncio
    async def test_bad_status_filter_is_a_400(self, db):
        app = _app(db)
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            resp = await ac.get("/api/v1/improve/proposals?status=whatever")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 11. The worker loop itself
# ---------------------------------------------------------------------------

class TestWorkerLifecycle:

    @pytest.mark.asyncio
    async def test_start_stop_is_clean_and_never_ticks_at_boot(self, db, repo):
        worker = Improver(db, Notifier(), proposer=_boom, repo_root_path=str(repo))
        await worker.start()
        assert worker._task is not None
        await worker.stop()
        assert worker._task is None
        assert db["improver_runs"].docs == []      # the 5-minute settle held

    @pytest.mark.asyncio
    async def test_interval_has_a_floor(self, db, repo, cfg):
        cfg["improver_interval_hours"] = 0
        worker = Improver(db, Notifier(), repo_root_path=str(repo))
        assert worker.interval >= 3600

    @pytest.mark.asyncio
    async def test_every_tick_is_recorded(self, db, repo, cfg, monkeypatch):
        monkeypatch.setattr(settings, "improver_enabled", True)
        cfg["improver_min_labelled_outcomes"] = 100
        worker = Improver(db, Notifier(), proposer=_boom, repo_root_path=str(repo))
        await worker.run_once()
        assert db["improver_runs"].docs[0]["status"] == "insufficient_data"
