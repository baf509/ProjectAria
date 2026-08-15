"""Tests for the steward's ResearchPlanner (steward proposal §5).

The invariants under test are the ones the design exists to enforce, not the
plumbing:
- an empty completion from a reasoning model is a FAILURE, never a zero result
  (this exact bug made DS4 label every memory with zero entities)
- the same question in different words must not be researched twice inside the
  cool-down — unless the charter changed since
- a per-project weekly budget, a token cap and a wall clock that actually stop a
  run (`total_tokens` was summed and never checked)
- a fabricated URL or quote must not be publishable: zero verified citations =
  memory yes, vault no
- nothing the planner emits may ever page Ben (`needs_human=True`)
- a research run must never be launched at an unpinned local endpoint, because
  that auto-routes to DS4 — pi's single coding slot

Everything runs against an in-memory Mongo stand-in; no network, no aria-api,
no Mongo, and the Signal transport is nailed shut.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

from aria.notifications import signal_rpc
from aria.notifications.service import NotificationService
from aria.planning.models import Charter, CharterBudget, CharterCadence, Project
from aria.steward.research import (
    Candidate,
    EmptyCompletion,
    ResearchPlanner,
    claim_supported,
    normalize_question,
    topic_hash,
)


# ---------------------------------------------------------------------------
# Minimal in-memory Mongo stand-in (no mongomock in this venv), same shape as
# tests/test_alerts_v2.py plus dotted keys, $exists, $gte and count_documents.
# ---------------------------------------------------------------------------

def _get(doc: dict, key: str):
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _match(doc: dict, flt: dict) -> bool:
    for key, expected in (flt or {}).items():
        if key == "$or":
            if not any(_match(doc, sub) for sub in expected):
                return False
            continue
        actual = _get(doc, key)
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
                elif op == "$lte":
                    if actual is None or actual > operand:
                        return False
                elif op == "$exists":
                    if (actual is not None) != bool(operand):
                        return False
                elif op == "$regex":
                    if actual is None or not re.search(operand, str(actual)):
                        return False
                else:  # pragma: no cover
                    raise NotImplementedError(op)
        elif actual != expected:
            return False
    return True


def _apply(doc: dict, update: dict) -> None:
    for op, fields in update.items():
        if op == "$set":
            for key, value in fields.items():
                if "." in key:
                    cur = doc
                    parts = key.split(".")
                    for part in parts[:-1]:
                        cur = cur.setdefault(part, {})
                    cur[parts[-1]] = value
                else:
                    doc[key] = value
        elif op == "$inc":
            for field, delta in fields.items():
                doc[field] = (doc.get(field) or 0) + delta
        elif op == "$max":
            for field, value in fields.items():
                current = doc.get(field)
                if current is None or value > current:
                    doc[field] = value
        else:  # pragma: no cover
            raise NotImplementedError(op)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction=1):
        self._docs.sort(
            key=lambda d: (_get(d, field) is None, _get(d, field) or 0),
            reverse=direction < 0,
        )
        return self

    def limit(self, n):
        self._docs = self._docs[: int(n)]
        return self

    async def to_list(self, length=None):
        return self._docs if length is None else self._docs[:length]


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = list(docs or [])

    async def insert_one(self, doc):
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def find_one(self, flt=None, *args, **kwargs):
        for doc in self.docs:
            if _match(doc, flt or {}):
                return doc
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
            return SimpleNamespace(matched_count=0, upserted_id=doc.get("_id"))
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def find_one_and_update(self, flt, update, sort=None, return_document=None, **kwargs):
        candidates = [d for d in self.docs if _match(d, flt)]
        if sort:
            field, direction = sort[0]
            candidates.sort(key=lambda d: _get(d, field) or 0, reverse=direction < 0)
        if not candidates:
            return None
        _apply(candidates[0], update)
        return dict(candidates[0])

    async def count_documents(self, flt=None, **kwargs):
        return sum(1 for d in self.docs if _match(d, flt or {}))

    def find(self, flt=None, *args, **kwargs):
        return _FakeCursor([d for d in self.docs if _match(d, flt or {})])


class FakeDB:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, FakeCollection())

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


@pytest.fixture(autouse=True)
def _no_real_signal():
    """corsair's .env carries a live Signal account and the signal-cli daemon is
    really listening, so an unpatched alert path in a test sends Ben an actual
    message (it has). See tests/test_alerts_v2.py."""

    class _Exploding:
        def __init__(self, *a, **k):
            raise AssertionError("a test must never open a Signal connection")

    with patch.object(signal_rpc, "httpx", SimpleNamespace(AsyncClient=_Exploding)):
        yield


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_project(**kw) -> Project:
    charter = kw.pop("charter", None) or Charter(
        purpose="Keep ARIA's local models fast enough to run unattended overnight.",
        goals=["cut cold prefill time"],
        research_topics=["llama.cpp slot scheduling under concurrent prefill"],
        cadence=CharterCadence(steward="30m", research="weekly"),
        budget=CharterBudget(research_runs_per_week=2),
    )
    now = _now()
    return Project(
        id=kw.pop("id", "proj-1"),
        name=kw.pop("name", "ProjectAria"),
        slug=kw.pop("slug", "projectaria"),
        path=kw.pop("path", "/home/ben/Development/ProjectAria"),
        created_at=now,
        updated_at=now,
        charter=charter,
        **kw,
    )


class FakeResearchService:
    """Stands in for aria.research.service.ResearchService.

    Signature-compatible with today's `start_research` — deliberately WITHOUT
    an `endpoint` parameter unless a test opts in, because that gap is what the
    planner's DS4 guard keys off.
    """

    def __init__(self, db, *, supports_endpoint=False, on_start=None):
        self.db = db
        self.calls: list[dict] = []
        self.task_runner = SimpleNamespace(cancel_task=AsyncMock(return_value=True))
        self.on_start = on_start
        if supports_endpoint:
            async def start_research(query, depth=2, breadth=3, model=None,
                                     backend=None, endpoint=None, **kw):
                return await self._start(query=query, depth=depth, breadth=breadth,
                                         model=model, backend=backend, endpoint=endpoint)
            self.start_research = start_research
        else:
            async def start_research(query, depth=2, breadth=3, model=None,
                                     backend=None, conversation_id=None):
                return await self._start(query=query, depth=depth, breadth=breadth,
                                         model=model, backend=backend)
            self.start_research = start_research

    async def _start(self, **kw):
        self.calls.append(kw)
        research_id = f"run-{len(self.calls)}"
        await self.db.research_runs.insert_one({
            "_id": research_id,
            "query": kw["query"],
            "status": "queued",
            "task_id": f"task-{len(self.calls)}",
            "model": kw.get("model"),
            "total_tokens": 0,
            "sources": [],
            "learnings": [],
            "created_at": _now(),
        })
        if self.on_start:
            await self.on_start(research_id)
        else:
            # Terminal by default. A run left "queued" makes the planner's
            # budget watchdog poll until its wall clock expires, which in a test
            # reads as a hang rather than a failure.
            await self.db.research_runs.update_one(
                {"_id": research_id}, {"$set": {"status": "completed"}}
            )
        return {"research_id": research_id, "task_id": f"task-{len(self.calls)}"}


class FakeWeb:
    """Stand-in for WebTool: url -> page body, or None for "does not resolve"."""

    def __init__(self, pages: dict[str, str | None]):
        self.pages = pages
        self.fetched: list[str] = []

    async def execute(self, args):
        url = args.get("url")
        self.fetched.append(url)
        body = self.pages.get(url)
        if body is None:
            return SimpleNamespace(status=SimpleNamespace(value="error"), output=None)
        return SimpleNamespace(status=SimpleNamespace(value="success"),
                               output={"content": body})


class FakeWriter:
    def __init__(self, path="/vault/ProjectAria/Research/note.md"):
        self.path = path
        self.published: list[dict] = []
        self.appended: list[dict] = []

    async def publish(self, content, *, title, doc_type="Research", project=None,
                      frontmatter=None, filename=None):
        self.published.append({
            "content": content, "title": title, "doc_type": doc_type,
            "project": project, "frontmatter": frontmatter or {},
        })
        return self.path

    async def append_section(self, path, heading, content, *, project=None, doc_type="Planning"):
        self.appended.append({"path": path, "heading": heading, "content": content})
        return path


class RecordingNotifier:
    def __init__(self):
        self.alerts: list[dict] = []

    async def notify(self, **kw):
        self.alerts.append(kw)
        return {"queued": True}


def make_planner(db, **kw) -> ResearchPlanner:
    kw.setdefault("research", FakeResearchService(db))
    kw.setdefault("notifier", RecordingNotifier())
    kw.setdefault("writer", FakeWriter())
    kw.setdefault("web", FakeWeb({}))
    kw.setdefault("poll_seconds", 0)
    return ResearchPlanner(db, **kw)


# ---------------------------------------------------------------------------
# 1. Topic hashing
# ---------------------------------------------------------------------------

class TestTopicHash:
    def test_stable_across_surface_noise(self):
        a = topic_hash("How does llama.cpp schedule slots under concurrent prefill?")
        b = topic_hash("how  does LLAMA.CPP schedule slots under concurrent prefill")
        assert a == b

    def test_word_order_is_identity(self):
        # "rocm on windows" and "windows on rocm" are different questions; a
        # bag-of-words hash would collapse them and silently skip one.
        assert topic_hash("ROCm support on Windows") != topic_hash("Windows support on ROCm")

    def test_filler_words_do_not_change_identity(self):
        assert topic_hash("What is the best approach to slot scheduling?") == topic_hash(
            "best approach slot scheduling"
        )

    def test_normalization_drops_punctuation(self):
        assert normalize_question("FP4/ROCm: does it work?") == "fp4 rocm work"


# ---------------------------------------------------------------------------
# 2. Question generation
# ---------------------------------------------------------------------------

class TestQuestionGeneration:
    async def test_uses_model_output_and_charter_context(self):
        db = FakeDB()
        project = make_project()
        await db.dream_journal.insert_one({
            "knowledge_gaps": ["How does ARIA's local model handle concurrent prefill pressure?"],
            "created_at": _now() - timedelta(days=1),
        })
        planner = make_planner(db)
        captured = {}

        async def fake_complete(prompt, *, max_tokens, temperature=0.4):
            captured["prompt"] = prompt
            return (
                '[{"question": "How do llama.cpp slot budgets interact with '
                'unified memory on Strix Halo?", "why": "slot 2 contention"}]'
            )

        with patch.object(planner, "_complete", side_effect=fake_complete):
            candidates = await planner.generate_questions(project)

        assert candidates
        assert any(c.origin == "model" for c in candidates)
        # The charter's own seeds ride along, so a model that returns one
        # question does not shrink the candidate set to one.
        assert any(c.origin == "charter_topic" for c in candidates)
        assert "concurrent prefill pressure" in captured["prompt"]  # dream gap reached the prompt
        assert "llama.cpp slot scheduling" in captured["prompt"]    # charter topic reached it

    async def test_empty_completion_falls_back_to_seeds_and_never_returns_nothing(self):
        """The DS4 bug in miniature: a reasoning model that spent its budget on
        reasoning_content returns "" — which must be an error, not a result."""
        db = FakeDB()
        project = make_project()
        notifier = RecordingNotifier()
        planner = make_planner(db, notifier=notifier)

        with patch.object(planner, "_complete", side_effect=EmptyCompletion("empty content")):
            candidates = await planner.generate_questions(project)

        assert candidates, "an outage must not look like 'nothing to research'"
        assert all(c.origin != "model" for c in candidates)
        assert any(a["event_type"] == "questions_fallback" for a in notifier.alerts)

    async def test_unparseable_json_falls_back(self):
        db = FakeDB()
        planner = make_planner(db)
        with patch.object(planner, "_complete", return_value="I think we should look into..."):
            candidates = await planner.generate_questions(make_project())
        assert candidates and all(c.origin != "model" for c in candidates)

    async def test_reasoning_preamble_before_json_is_recovered(self):
        db = FakeDB()
        planner = make_planner(db)
        answer = (
            "Let me think about what matters here.\n"
            '[{"question": "What limits ROCm FP4 throughput on gfx1151 in 2026?"}]'
        )
        with patch.object(planner, "_complete", return_value=answer):
            candidates = await planner.generate_questions(make_project())
        assert candidates[0].origin == "model"

    async def test_gaps_unrelated_to_the_project_are_not_offered(self):
        db = FakeDB()
        await db.dream_journal.insert_one({
            "knowledge_gaps": ["What is the resale value of a 2019 kayak?"],
            "created_at": _now() - timedelta(days=1),
        })
        planner = make_planner(db)
        context = await planner._gather_context(make_project())
        assert context["gaps"] == []


# ---------------------------------------------------------------------------
# 3. Dedup + cool-down
# ---------------------------------------------------------------------------

class TestDedupAndCooldown:
    async def test_recent_run_blocks_the_same_topic_in_other_words(self):
        db = FakeDB()
        project = make_project()
        await db.research_runs.insert_one({
            "_id": "run-old",
            "query": "How does llama.cpp schedule slots under concurrent prefill?",
            "created_at": _now() - timedelta(days=3),
            "planner": {"project_slug": project.slug,
                        "topic_hash": topic_hash(
                            "How does llama.cpp schedule slots under concurrent prefill?")},
        })
        planner = make_planner(db)
        accepted, rejected = await planner.filter_candidates(project, [
            Candidate(question="how does LLAMA.CPP schedule slots under concurrent prefill",
                      origin="model"),
            Candidate(question="What changed in ROCm 7.2 for gfx1201?", origin="model"),
        ])
        assert [c.question for c in accepted] == ["What changed in ROCm 7.2 for gfx1201?"]
        assert rejected[0]["reason"].startswith("cooldown")

    async def test_research_memories_also_consume_the_cooldown(self):
        db = FakeDB()
        project = make_project()
        await db.memories.insert_one({
            "source": {"type": "research", "query": "ROCm 7.2 gfx1201 changes"},
            "created_at": _now() - timedelta(days=2),
        })
        planner = make_planner(db)
        accepted, rejected = await planner.filter_candidates(
            project, [Candidate(question="ROCm 7.2 gfx1201 changes", origin="model")]
        )
        assert accepted == []
        assert rejected[0]["reason"] == "cooldown:memory"

    async def test_run_outside_the_window_does_not_block(self):
        db = FakeDB()
        project = make_project()
        await db.research_runs.insert_one({
            "_id": "run-ancient",
            "query": "ROCm 7.2 gfx1201 changes",
            "created_at": _now() - timedelta(days=400),
            "planner": {"project_slug": project.slug,
                        "topic_hash": topic_hash("ROCm 7.2 gfx1201 changes")},
        })
        planner = make_planner(db)
        accepted, _ = await planner.filter_candidates(
            project, [Candidate(question="ROCm 7.2 gfx1201 changes", origin="model")]
        )
        assert len(accepted) == 1

    async def test_charter_amended_after_the_run_waives_the_cooldown(self):
        db = FakeDB()
        project = make_project()
        project.charter.approved_at = _now() - timedelta(days=1)
        await db.research_runs.insert_one({
            "_id": "run-old",
            "query": "ROCm 7.2 gfx1201 changes",
            "created_at": _now() - timedelta(days=5),
            "planner": {"project_slug": project.slug,
                        "topic_hash": topic_hash("ROCm 7.2 gfx1201 changes")},
        })
        planner = make_planner(db)
        accepted, _ = await planner.filter_candidates(
            project, [Candidate(question="ROCm 7.2 gfx1201 changes", origin="model")]
        )
        assert len(accepted) == 1, "a rewritten charter re-opens old questions"

    async def test_another_projects_run_does_not_block_this_one(self):
        db = FakeDB()
        project = make_project()
        await db.research_runs.insert_one({
            "_id": "run-other",
            "query": "ROCm 7.2 gfx1201 changes",
            "created_at": _now() - timedelta(days=1),
            "planner": {"project_slug": "some-other-project",
                        "topic_hash": topic_hash("ROCm 7.2 gfx1201 changes")},
        })
        planner = make_planner(db)
        accepted, _ = await planner.filter_candidates(
            project, [Candidate(question="ROCm 7.2 gfx1201 changes", origin="model")]
        )
        assert len(accepted) == 1

    async def test_duplicates_within_one_batch_collapse(self):
        db = FakeDB()
        planner = make_planner(db)
        accepted, rejected = await planner.filter_candidates(make_project(), [
            Candidate(question="ROCm 7.2 gfx1201 changes", origin="model"),
            Candidate(question="rocm 7.2 gfx1201 changes?", origin="charter_topic"),
        ])
        assert len(accepted) == 1
        assert rejected[0]["reason"] == "duplicate_in_batch"


# ---------------------------------------------------------------------------
# 4. Budget
# ---------------------------------------------------------------------------

class TestBudget:
    async def test_weekly_cap_is_enforced_per_project(self):
        db = FakeDB()
        project = make_project()
        for i in range(2):
            await db.research_runs.insert_one({
                "_id": f"run-{i}", "query": f"q{i}", "created_at": _now() - timedelta(days=i),
                "planner": {"project_slug": project.slug, "topic_hash": f"h{i}"},
            })
        planner = make_planner(db)
        state = await planner.budget_state(project)
        assert state == {**state, "runs_per_week": 2, "runs_used": 2, "exhausted": True}

        result = await planner.run_project(project)
        assert result["status"] == "skipped" and result["reason"] == "budget_exhausted"

    async def test_runs_older_than_a_week_do_not_count(self):
        db = FakeDB()
        project = make_project()
        await db.research_runs.insert_one({
            "_id": "old", "query": "q", "created_at": _now() - timedelta(days=9),
            "planner": {"project_slug": project.slug, "topic_hash": "h"},
        })
        state = await make_planner(db).budget_state(project)
        assert state["runs_used"] == 0 and state["exhausted"] is False

    async def test_token_cap_cancels_a_running_run(self):
        """`total_tokens` has always been summed and never checked. The planner
        is what stops a runaway run — from outside, by cancelling its task."""
        db = FakeDB()
        project = make_project()
        research = FakeResearchService(db, supports_endpoint=True)

        async def blow_the_budget(research_id):
            await db.research_runs.update_one(
                {"_id": research_id}, {"$set": {"status": "running", "total_tokens": 999_999}}
            )

        research.on_start = blow_the_budget
        planner = make_planner(db, research=research)
        with patch.object(planner, "generate_questions",
                          return_value=[Candidate(question="Does ROCm 7.2 change FP4 throughput?",
                                                  origin="model")]):
            result = await planner.run_project(project)

        assert result["status"] == "failed"
        run = await db.research_runs.find_one({"_id": "run-1"})
        assert run["status"] == "cancelled"
        assert run["budget_breach"].startswith("tokens>")
        research.task_runner.cancel_task.assert_awaited_once_with("task-1")

    async def test_wall_clock_cancels_a_stuck_run(self):
        db = FakeDB()
        project = make_project()
        research = FakeResearchService(db, supports_endpoint=True)

        async def never_finish(research_id):
            await db.research_runs.update_one({"_id": research_id}, {"$set": {"status": "running"}})

        research.on_start = never_finish
        planner = make_planner(db, research=research)
        with patch("aria.steward.research._setting", side_effect=lambda n, d:
                   0 if n == "research_planner_max_wall_minutes" else d):
            with patch.object(planner, "generate_questions",
                              return_value=[Candidate(question="Does ROCm 7.2 change FP4?",
                                                      origin="model")]):
                result = await planner.run_project(project)
        assert result["status"] == "failed"
        run = await db.research_runs.find_one({"_id": "run-1"})
        assert run["budget_breach"].startswith("wall>")


# ---------------------------------------------------------------------------
# 5. Citation check
# ---------------------------------------------------------------------------

class TestClaimSupported:
    def test_quote_present_verbatim_verifies(self):
        ok, reason = claim_supported(
            'The docs say "slots share a single KV cache pool" for this build.',
            "<p>In this server, slots share a single KV cache pool by default.</p>",
        )
        assert ok and reason == "quote_verified"

    def test_fabricated_quote_fails_even_when_the_topic_matches(self):
        ok, reason = claim_supported(
            'The docs say "slots each get a private KV cache pool" here.',
            "In this server, slots share a single KV cache pool by default.",
        )
        assert not ok and reason == "quote_not_found"

    def test_invented_number_fails(self):
        ok, reason = claim_supported(
            "ROCm 7.2 raises FP4 throughput to 850 tokens per second on gfx1201.",
            "ROCm 7.2 raises FP4 throughput on gfx1201; measured at 214 tokens per second.",
        )
        assert not ok and reason.startswith("number_not_found")

    def test_paraphrase_with_enough_overlap_verifies(self):
        ok, reason = claim_supported(
            "Concurrent prefill on a second slot degrades decode throughput for the first slot.",
            "When a second slot begins prefill, decode throughput for the first slot degrades "
            "sharply until the prefill completes.",
        )
        assert ok and reason.startswith("overlap")

    def test_generic_claim_is_unverifiable_and_therefore_unverified(self):
        ok, reason = claim_supported("It is fast.", "It is fast.")
        assert not ok and reason == "claim_too_generic"


class TestCitationCheck:
    async def test_dead_url_counts_as_unverified(self):
        db = FakeDB()
        planner = make_planner(db, web=FakeWeb({
            "https://real.example/a":
                "<p>In this server build, slots share a KV pool across requests.</p>",
        }))
        run = {"learnings": [
            {"content": "Slots share a KV pool across requests in this server build.",
             "source_url": "https://real.example/a"},
            {"content": "Prefill is batched at 4096 tokens per chunk by default.",
             "source_url": "https://invented.example/nope"},
        ]}
        result = await planner.verify_citations(run)
        assert result["claimed"] == 2
        assert result["verified"] == 1
        assert result["urls_dead"] == ["https://invented.example/nope"]

    async def test_each_url_is_fetched_once_even_when_cited_repeatedly(self):
        db = FakeDB()
        web = FakeWeb({"https://a.example": "slots share a single KV cache pool always"})
        planner = make_planner(db, web=web)
        run = {"learnings": [
            {"content": "Slots share a single KV cache pool.", "source_url": "https://a.example"},
            {"content": "The KV cache pool is shared between slots.", "source_url": "https://a.example"},
        ]}
        await planner.verify_citations(run)
        assert web.fetched == ["https://a.example"]


# ---------------------------------------------------------------------------
# 6. Publish gating
# ---------------------------------------------------------------------------

class TestPublishGate:
    async def _completed_run(self, db, *, learnings, sources):
        research = FakeResearchService(db, supports_endpoint=True)

        async def finish(research_id):
            await db.research_runs.update_one({"_id": research_id}, {"$set": {
                "status": "completed",
                "report_text": "A synthesis of what was found.",
                "learnings": learnings,
                "sources": sources,
                "total_tokens": 1234,
                "model": "qwen3.8-27b-rocmfp4-r9700",
            }})

        research.on_start = finish
        return research

    async def test_zero_verified_citations_is_not_published(self):
        db = FakeDB()
        research = await self._completed_run(
            db,
            learnings=[{"content": "FP4 throughput reaches 850 t/s on gfx1201.",
                        "source_url": "https://invented.example/post"}],
            sources=[{"url": "https://invented.example/post", "title": "Invented"}],
        )
        writer, notifier = FakeWriter(), RecordingNotifier()
        planner = make_planner(db, research=research, writer=writer, notifier=notifier,
                               web=FakeWeb({}))
        with patch.object(planner, "generate_questions",
                          return_value=[Candidate(question="What is FP4 throughput on gfx1201?",
                                                  origin="model")]):
            result = await planner.run_project(make_project())

        assert result["status"] == "completed"
        assert result["vault_path"] is None
        assert writer.published == [], "an unverified note must not reach the vault"
        assert any(a["event_type"] == "citations_unverified" for a in notifier.alerts)
        run = await db.research_runs.find_one({"_id": "run-1"})
        assert run["sources_verified"] == "0/1"

    async def test_verified_run_publishes_with_pending_frontmatter(self):
        db = FakeDB()
        page = "Concurrent prefill on a second slot degrades decode throughput for the first slot."
        research = await self._completed_run(
            db,
            learnings=[{"content": "Concurrent prefill on a second slot degrades decode "
                                   "throughput for the first slot.",
                        "source_url": "https://real.example/slots"}],
            sources=[{"url": "https://real.example/slots", "title": "Slots"}],
        )
        writer = FakeWriter()
        planner = make_planner(db, research=research, writer=writer,
                               web=FakeWeb({"https://real.example/slots": page}))
        question = "How does concurrent prefill affect decode on a shared server?"
        with patch.object(planner, "generate_questions",
                          return_value=[Candidate(question=question, origin="model")]):
            result = await planner.run_project(make_project())

        assert result["vault_path"] == writer.path
        published = writer.published[0]
        assert published["doc_type"] == "Research"
        assert published["project"] == "/home/ben/Development/ProjectAria"
        fm = published["frontmatter"]
        assert fm["accepted"] == "pending"
        assert fm["topic_hash"] == topic_hash(question)
        assert fm["sources_verified"] == "1/1"
        assert fm["model"] == "qwen3.8-27b-rocmfp4-r9700"
        assert "verified" in published["content"]
        # A one-line entry lands in the project's steward plan.
        assert writer.appended and writer.appended[0]["path"] == "STEWARD_PLAN.md"
        run = await db.research_runs.find_one({"_id": "run-1"})
        assert run["vault_path"] == writer.path and run["accepted"] == "pending"

    async def test_planner_metadata_lands_on_the_run_document(self):
        db = FakeDB()
        research = await self._completed_run(db, learnings=[], sources=[])
        planner = make_planner(db, research=research)
        with patch.object(planner, "generate_questions",
                          return_value=[Candidate(question="What limits FP4 decode on gfx1151?",
                                                  origin="charter_topic")]):
            await planner.run_project(make_project())
        run = await db.research_runs.find_one({"_id": "run-1"})
        assert run["planner"]["project_slug"] == "projectaria"
        assert run["topic_hash"] == topic_hash("What limits FP4 decode on gfx1151?")
        assert run["planner"]["endpoint"].endswith("/v1")


# ---------------------------------------------------------------------------
# 7. Model routing and slot rules
# ---------------------------------------------------------------------------

class TestModelRules:
    async def test_unpinned_endpoint_refuses_to_launch(self):
        """ResearchService without an `endpoint` parameter resolves the local
        adapter through the /llm/v1 proxy, which auto-routes to DS4 — pi's only
        slot. Refusing is the point."""
        db = FakeDB()
        research = FakeResearchService(db, supports_endpoint=False)
        notifier = RecordingNotifier()
        planner = make_planner(db, research=research, notifier=notifier)
        with patch("aria.core.claude_runner.ClaudeRunner.is_available", return_value=False):
            with patch.object(planner, "generate_questions",
                              return_value=[Candidate(question="Anything at all here?",
                                                      origin="model")]):
                result = await planner.run_project(make_project())

        assert result["status"] == "skipped"
        assert result["reason"] == "endpoint_unpinned"
        assert research.calls == []
        assert any(a["event_type"] == "launch_blocked" for a in notifier.alerts)

    async def test_pinned_endpoint_is_passed_through(self):
        db = FakeDB()
        research = FakeResearchService(db, supports_endpoint=True)
        planner = make_planner(db, research=research)
        with patch.object(planner, "generate_questions",
                          return_value=[Candidate(question="What limits FP4 decode on gfx1151?",
                                                  origin="model")]):
            await planner.run_project(make_project())
        call = research.calls[0]
        assert call["endpoint"] == planner.endpoint
        assert call["model"] == planner.model
        assert "8108" not in str(call["endpoint"]), "8108 is DS4 — never background work"

    def test_night_window_allows_heavy_prefill(self):
        planner = make_planner(FakeDB())
        heavy, reason = planner._heavy_allowed(datetime(2026, 8, 15, 3, 0).astimezone())
        assert heavy and reason == "night_window"

    def test_daytime_with_active_hermes_stays_constrained(self):
        planner = make_planner(FakeDB())
        planner._hermes_idle_cached = 2.0
        heavy, reason = planner._heavy_allowed(datetime(2026, 8, 15, 14, 0).astimezone())
        assert not heavy and reason == "daytime_shared_slot"

    def test_daytime_with_idle_hermes_allows_heavy(self):
        planner = make_planner(FakeDB())
        planner._hermes_idle_cached = 45.0
        heavy, reason = planner._heavy_allowed(datetime(2026, 8, 15, 14, 0).astimezone())
        assert heavy and reason.startswith("hermes_idle")

    def test_no_evidence_of_hermes_activity_is_treated_as_busy(self):
        planner = make_planner(FakeDB())
        planner._hermes_idle_cached = None
        heavy, _ = planner._heavy_allowed(datetime(2026, 8, 15, 14, 0).astimezone())
        assert not heavy

    def test_run_shape_stays_within_the_source_cap(self):
        planner = make_planner(FakeDB())
        budget = {"max_sources": 4}
        depth, breadth = planner._run_shape(True, budget)
        assert depth * breadth <= 4
        day_depth, day_breadth = planner._run_shape(False, budget)
        assert day_depth * day_breadth <= depth * breadth


# ---------------------------------------------------------------------------
# 8. Nothing here may page Ben
# ---------------------------------------------------------------------------

class TestNeverPagesBen:
    async def test_every_planner_alert_is_needs_human_false(self):
        """`classify()` sends an unknown source to needs_human=True, so a
        planner alert that forgot to classify itself would Signal Ben on every
        finished research note."""
        db = FakeDB()
        planner = make_planner(db, notifier=NotificationService())
        project = make_project()
        with patch("aria.db.mongodb.get_database", new=AsyncMock(return_value=db)):
            for event in ("published", "citations_unverified", "launch_blocked",
                          "questions_fallback", "run_incomplete"):
                await planner._log_event(project, event, "detail")

        assert len(db.alerts.docs) == 5
        assert all(a["needs_human"] is False for a in db.alerts.docs)
        assert all(a["kind"] == "research" for a in db.alerts.docs)
        assert all(a["project_slug"] == "projectaria" for a in db.alerts.docs)


# ---------------------------------------------------------------------------
# 9. Cadence / active-set gating
# ---------------------------------------------------------------------------

class TestTickGating:
    async def test_manual_cadence_is_never_auto_run(self):
        db = FakeDB()
        project = make_project()
        project.charter.cadence = CharterCadence(steward="30m", research="manual")
        planner = make_planner(db)
        with patch.object(planner.planning, "active_projects", return_value=[project]):
            out = await planner.tick()
        assert out["ran"] == 0
        assert out["results"][0]["reason"] == "cadence_manual"

    async def test_paused_steward_stops_spending_the_research_budget(self):
        db = FakeDB()
        project = make_project()
        project.steward = SimpleNamespace(paused_reason="pause proposed", enabled=True)
        planner = make_planner(db)
        with patch.object(planner.planning, "active_projects", return_value=[project]):
            out = await planner.tick()
        assert out["results"][0]["reason"] == "steward_paused"

    async def test_weekly_cadence_not_due_yet(self):
        db = FakeDB()
        project = make_project()
        await db.research_runs.insert_one({
            "_id": "recent", "query": "q", "created_at": _now() - timedelta(days=2),
            "planner": {"project_slug": project.slug, "topic_hash": "h"},
        })
        planner = make_planner(db)
        with patch.object(planner.planning, "active_projects", return_value=[project]):
            out = await planner.tick()
        assert out["results"][0]["reason"] == "cadence_not_due"

    async def test_tick_runs_one_project_per_pass(self):
        db = FakeDB()
        projects = [make_project(id="p1", slug="a", name="A"),
                    make_project(id="p2", slug="b", name="B")]
        planner = make_planner(db)
        with patch.object(planner.planning, "active_projects", return_value=projects):
            with patch.object(planner, "run_project",
                              return_value={"status": "completed"}) as run:
                out = await planner.tick()
        assert run.await_count == 1 and out["ran"] == 1

    async def test_project_without_a_charter_purpose_is_skipped(self):
        db = FakeDB()
        project = make_project(charter=Charter(purpose="   "))
        result = await make_planner(db).run_project(project)
        assert result["reason"] == "no_charter_purpose"

    async def test_dry_run_plans_without_launching(self):
        db = FakeDB()
        research = FakeResearchService(db, supports_endpoint=True)
        planner = make_planner(db, research=research)
        with patch.object(planner, "generate_questions",
                          return_value=[Candidate(question="What limits FP4 decode on gfx1151?",
                                                  origin="model")]):
            result = await planner.run_project(make_project(), dry_run=True)
        assert result["status"] == "planned" and research.calls == []
        assert result["topic_hash"] == topic_hash("What limits FP4 decode on gfx1151?")


# ---------------------------------------------------------------------------
# 10. Scheduler action
# ---------------------------------------------------------------------------

class TestSchedulerResearchAction:
    async def _scheduler(self, db):
        from aria.scheduler.service import SchedulerService

        submitted: list[dict] = []

        class _Runner:
            async def submit_task(self, *, name, coroutine_factory, notify=True,
                                  metadata=None, timeout_seconds=None):
                submitted.append({"name": name, "notify": notify, "metadata": metadata,
                                  "timeout_seconds": timeout_seconds,
                                  "factory": coroutine_factory})
                return "task-x"

        scheduler = SchedulerService(db, _Runner(), RecordingNotifier())
        return scheduler, submitted

    async def test_research_schedule_submits_a_planner_run(self):
        db = FakeDB()
        scheduler, submitted = await self._scheduler(db)
        schedule = {
            "_id": ObjectId(),
            "name": "weekly research: projectaria",
            "schedule_type": "recurring",
            "cron_expr": "weekly sunday 02:00",
            "action": "research",
            "params": {"project": "projectaria"},
            "next_run_at": _now() - timedelta(minutes=1),
            "run_count": 0,
        }
        await db.schedules.insert_one(schedule)

        planner_calls = {}

        class _Planner:
            def __init__(self, db, **kw):
                planner_calls["kwargs"] = kw

            async def run_project(self, project, **kw):
                planner_calls["project"] = project
                planner_calls["kw"] = kw
                return {"status": "completed"}

        await scheduler._execute_schedule(schedule)

        assert len(submitted) == 1
        assert submitted[0]["name"] == "research-plan:projectaria"
        # notify=False: the task runner's own completion/failure notices go out
        # as source="task", which classifies to needs_human=True.
        assert submitted[0]["notify"] is False
        assert submitted[0]["metadata"]["task_kind"] == "scheduled_research"

        with patch("aria.steward.research.ResearchPlanner", _Planner):
            with patch("aria.api.deps.get_research_service",
                       new=AsyncMock(return_value=FakeResearchService(db))):
                with patch("aria.api.deps.get_notification_service",
                           return_value=RecordingNotifier()):
                    out = await submitted[0]["factory"]()
        assert out == {"status": "completed"}
        assert planner_calls["project"] == "projectaria"

        # The recurring row is rescheduled, not left to re-fire every tick.
        stored = await db.schedules.find_one({"_id": schedule["_id"]})
        assert stored["run_count"] == 1 and stored["next_run_at"] > _now()

    async def test_research_schedule_without_a_project_is_a_no_op(self):
        db = FakeDB()
        scheduler, submitted = await self._scheduler(db)
        schedule = {
            "_id": ObjectId(), "name": "bad", "schedule_type": "once",
            "action": "research", "params": {}, "next_run_at": _now(), "run_count": 0,
        }
        await db.schedules.insert_one(schedule)
        await scheduler._execute_schedule(schedule)
        assert submitted == []
