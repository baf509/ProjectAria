"""Tests for outcome scoring, the different-family review, and the re-route rung.

The invariants under test are the ones that were actually broken, not stylistic:

- a session's label must come from evidence, never from its own claim of success
  (14/17 sessions are `stopped`, every report says `partial`, and `exit_code=0`
  is currently enough to read as `completed` even when pi crashed in 3 seconds)
- an empty model reply is a FAILURE, never a passing review (a reasoning model
  spends its budget on reasoning_content and returns content="" — the exact bug
  that made DS4 label every memory with zero entities)
- a reviewer from the author's own model family is not an independent check
- unknown token counts stay None, never 0, or "$ per merged change" silently
  reports a discount that did not happen
- re-routing only ever moves UP the ladder, only into charter-allowed tiers, and
  never into a launch profile that does not exist on this box

No network, no Mongo, no aria-api: the DB is the in-memory fake below and every
model call is patched. Nothing here can reach aria.notifications.signal_rpc.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aria.agents import review as review_mod
from aria.agents import routing
from aria.agents.review import (
    FAMILY_CLOUD,
    FAMILY_DS4,
    FAMILY_QWEN,
    CodingReviewService,
    _parse_review,
    model_family,
    pick_reviewer_family,
)
from aria.llm import pricing
from aria.steward import outcomes as outcomes_mod
from aria.steward.outcomes import OutcomeScorer, OutcomeWorker, metrics, scan_pi_jsonl


# ---------------------------------------------------------------------------
# Minimal in-memory Mongo stand-in (no mongomock in this venv)
# ---------------------------------------------------------------------------

def _match(doc: dict, flt: dict) -> bool:
    for key, expected in (flt or {}).items():
        actual = doc.get(key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if op == "$in":
                    if actual not in operand:
                        return False
                elif op == "$ne":
                    if actual == operand:
                        return False
                elif op == "$exists":
                    if (key in doc) != bool(operand):
                        return False
                else:  # pragma: no cover - unsupported operator in a test
                    raise NotImplementedError(op)
        elif actual != expected:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction=1):
        self._docs.sort(key=lambda d: d.get(field) or 0, reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[: int(n)]
        return self

    async def to_list(self, length=None):
        return self._docs if length is None else self._docs[:length]


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = [dict(d) for d in (docs or [])]

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("_id"))

    async def find_one(self, flt=None, *args, **kwargs):
        for doc in self.docs:
            if _match(doc, flt or {}):
                return dict(doc)
        return None

    async def update_one(self, flt, update, upsert=False, **kwargs):
        for doc in self.docs:
            if _match(doc, flt):
                doc.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1)
        if upsert:
            doc = {k: v for k, v in flt.items() if not isinstance(v, dict)}
            doc.update(update.get("$set", {}))
            self.docs.append(doc)
            return SimpleNamespace(matched_count=0, upserted_id=doc.get("_id"))
        return SimpleNamespace(matched_count=0)

    def find(self, flt=None, *args, **kwargs):
        return _Cursor([dict(d) for d in self.docs if _match(d, flt or {})])

    async def count_documents(self, flt=None):
        return len([d for d in self.docs if _match(d, flt or {})])


class FakeDB:
    def __init__(self, **collections):
        self._colls: dict[str, FakeCollection] = {
            name: FakeCollection(docs) for name, docs in collections.items()
        }

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, FakeCollection())

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _session(**overrides) -> dict:
    doc = {
        "_id": "sess-1",
        "backend": "pi-code",
        "llm": "ds4",
        "model": "DS4-0731-UD-IQ3-XXS-Halo",
        "workspace": "/home/ben/Development/ProjectAria",
        "status": "completed",
        "exit_code": 0,
        "prompt": "fix the failing test",
        "loop_nudges": 0,
        "created_at": NOW - timedelta(minutes=30),
        "completed_at": NOW,
        "updated_at": NOW,
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------

class TestPricing:
    def test_routed_models_are_priced(self):
        # Both of these are what agents/routing.py actually routes to, and
        # NEITHER had an entry — every routed session was billed at the
        # UNKNOWN_CLOUD guess.
        assert pricing.price_for("claude-sonnet-5") == (3.0, 15.0)
        assert pricing.price_for("claude-opus-4-8") == (5.0, 25.0)
        assert pricing.price_for("claude-opus-5") == (5.0, 25.0)
        assert pricing.price_for("claude-haiku-4-5") == (1.0, 5.0)

    def test_suffixed_id_matches_the_most_specific_key(self):
        # Dict-order matching sent "claude-opus-4-8-20260101" to the
        # "claude-opus-4" entry and priced it at 3x its real rate.
        assert pricing.price_for("claude-opus-4-8-20260101") == (5.0, 25.0)
        assert pricing.price_for("claude-sonnet-5[1m]") == (3.0, 15.0)

    def test_local_models_and_backends_are_free(self):
        assert pricing.price_for("DS4-0731-UD-IQ3-XXS-Halo", "pi-code") == (0.0, 0.0)
        # Unknown backend, but the model id is unmistakably local: pi records
        # `provider: ds4`, which is not one of ARIA's adapter names.
        assert pricing.price_for("qwen3.8-27b-rocmfp4-r9700", "weird") == (0.0, 0.0)
        assert pricing.cost_for("DS4-0731-UD-IQ3-XXS-Halo", 100_000, 5_000, "ds4") == 0.0

    def test_unknown_cloud_still_falls_back(self):
        assert pricing.price_for("some-vendor/mystery-model") == pricing.UNKNOWN_CLOUD


# ---------------------------------------------------------------------------
# review — family correlation and parsing
# ---------------------------------------------------------------------------

class TestReviewFamilies:
    def test_family_detection(self):
        assert model_family("claude_code", "claude-sonnet-5") == FAMILY_CLOUD
        assert model_family("pi-code", "DS4-0731-UD-IQ3-XXS-Halo", "ds4") == FAMILY_DS4
        assert model_family("pi-code", "qwen3.8-27b-rocmfp4-r9700", "ridge") == FAMILY_QWEN
        assert model_family("codex", None) == "openai"

    def test_reviewer_never_shares_the_authors_family(self):
        # Configured reviewer is cloud; a cloud-authored diff must not be
        # "reviewed" by cloud — that cascade shares its blind spots.
        assert pick_reviewer_family(FAMILY_CLOUD, FAMILY_CLOUD) == FAMILY_QWEN
        assert pick_reviewer_family(FAMILY_DS4, FAMILY_CLOUD) == FAMILY_CLOUD

    def test_parse_rejects_empty_and_junk(self):
        # The DS4 bug: a reasoning model burns its budget on reasoning_content
        # and returns "". That must never be read as an approval.
        assert _parse_review("") is None
        assert _parse_review("   ") is None
        assert _parse_review("I think it looks fine!") is None
        assert _parse_review(json.dumps({"verdict": "shrug"})) is None

    def test_parse_accepts_fenced_json_and_flags_blocking(self):
        raw = "```json\n" + json.dumps({
            "verdict": "concerns",
            "confidence": 0.8,
            "summary": "drops a guard check",
            "findings": [{"severity": "high", "file": "a.py", "detail": "removes the estop check"}],
        }) + "\n```"
        parsed = _parse_review(raw)
        assert parsed["verdict"] == "concerns"
        # A high-severity finding is blocking even when the verdict is softer —
        # the merge gate keys off `blocking`, not the adjective.
        assert parsed["blocking"] is True


@pytest.mark.asyncio
async def test_review_diff_treats_empty_reply_as_failure():
    db = FakeDB()
    manager = SimpleNamespace(get_session=AsyncMock(return_value=_session()))
    service = CodingReviewService(db, manager)

    with patch.object(service, "_ask_reviewer", AsyncMock(return_value=("", {}, "claude-sonnet-5"))):
        result = await service.review_diff("sess-1", diff="--- a\n+++ b\n+x\n")

    assert result["ran"] is False
    assert "empty content" in result["reason"]
    assert "verdict" not in result  # nothing approving was written


@pytest.mark.asyncio
async def test_review_diff_records_verdict_and_usage():
    db = FakeDB()
    manager = SimpleNamespace(get_session=AsyncMock(return_value=_session()))
    service = CodingReviewService(db, manager)
    reply = json.dumps({
        "verdict": "approve", "confidence": 0.9, "summary": "ok", "findings": [],
    })

    with patch.object(
        service, "_ask_reviewer",
        AsyncMock(return_value=(reply, {"input_tokens": 900, "output_tokens": 60}, "claude-sonnet-5")),
    ):
        result = await service.review_diff("sess-1", diff="--- a\n+++ b\n+x\n")

    assert result["ran"] is True
    assert result["verdict"] == "approve"
    assert result["author_family"] == FAMILY_DS4
    assert result["reviewer_family"] == FAMILY_CLOUD
    assert result["independent"] is True
    stored = await db.session_reviews.find_one({"session_id": "sess-1"})
    assert stored["verdict"] == "approve"
    # Cloud review costs money and the weekly report divides dollars by merged
    # changes — an unbooked review makes every merge look cheaper than it was.
    usage = await db.usage.find_one({"source": "coding:review"})
    assert usage["input_tokens"] == 900


@pytest.mark.asyncio
async def test_review_never_sends_work_to_pis_single_slot():
    """DS4 is one 131K slot and pi lives in it — a review there evicts the
    coding agent's warm prefix (4.2s warm vs 39.5s cold)."""
    db = FakeDB()
    session = _session(backend="claude_code", model="claude-sonnet-5", llm=None)
    manager = SimpleNamespace(get_session=AsyncMock(return_value=session))
    service = CodingReviewService(db, manager)
    result = await service.review_diff(
        "sess-1", reviewer_family=FAMILY_DS4, diff="--- a\n+++ b\n+x\n"
    )
    assert result["ran"] is False
    assert "single coding slot" in result["reason"]
    # ...and it is never *chosen* automatically either.
    assert pick_reviewer_family(FAMILY_CLOUD, FAMILY_CLOUD) != FAMILY_DS4


@pytest.mark.asyncio
async def test_review_diff_refuses_empty_diff():
    db = FakeDB()
    manager = SimpleNamespace(get_session=AsyncMock(return_value=_session()))
    service = CodingReviewService(db, manager)
    result = await service.review_diff("sess-1", diff="   \n")
    assert result["ran"] is False
    assert "no diff" in result["reason"]


# ---------------------------------------------------------------------------
# routing — the re-route rung
# ---------------------------------------------------------------------------

def _ladder_without_remote():
    """The ladder as it behaves on a box with no ridge/red launch profiles."""
    return routing.default_ladder()


class TestClassifyTier:
    def test_tiers(self):
        assert routing.classify_tier({"backend": "claude_code", "model": "claude-opus-4-8"}) == "cloud"
        assert routing.classify_tier({"backend": "pi-code", "llm": "ds4"}) == "local"
        assert routing.classify_tier({"backend": "pi-code", "llm": "ridge"}) == "ridge"
        # codex is hosted, not a slot on this box — classifying it local would
        # make the ladder "promote" a failed codex session onto Ridge.
        assert routing.classify_tier({"backend": "codex", "model": None}) == "cloud"


@pytest.mark.asyncio
async def test_reroute_promotes_one_rung_and_honours_the_charter():
    db = FakeDB(agents=[{"slug": "pi-coding-ridge", "enabled": True}])
    session = _session()

    verdict = await routing.reroute(db, session, tiers_allowed=["local", "ridge", "cloud"])
    assert verdict is not None
    # One rung, not a jump to the strongest tier available.
    assert verdict.tier == "ridge"
    assert verdict.start_kwargs()["subagent_profile"] == "pi-coding-ridge"

    # With ridge disallowed by the charter, the next allowed rung is cloud.
    verdict = await routing.reroute(db, session, tiers_allowed=["local", "cloud"])
    assert verdict.tier == "cloud"
    assert verdict.start_kwargs()["model"] == routing.settings.coding_routing_model_standard


@pytest.mark.asyncio
async def test_reroute_skips_missing_launch_profiles():
    # pi-coding-red does not exist on this box; escalating into it would raise
    # "subagent profile not found" and turn a stall into a hard failure.
    db = FakeDB(agents=[])
    verdict = await routing.reroute(db, _session(), tiers_allowed=["ridge", "red", "cloud"])
    assert verdict.tier == "cloud"
    assert any("pi-coding-ridge" in s for s in verdict.skipped)


@pytest.mark.asyncio
async def test_reroute_never_demotes_and_can_exhaust():
    db = FakeDB()
    deep = _session(
        backend="claude_code", llm=None,
        model=routing.settings.coding_routing_model_deep,
    )
    assert await routing.reroute(db, deep) is None


@pytest.mark.asyncio
async def test_reroute_respects_quota_cooldown_and_prior_attempts():
    db = FakeDB(model_availability=[{
        "_id": routing.CLAUDE_PROVIDER,
        "cooled_until": datetime.now(timezone.utc) + timedelta(minutes=30),
    }])
    session = _session(reroute={"history": [{"tier": "ridge"}]})
    verdict = await routing.reroute(db, session, tiers_allowed=["ridge", "cloud"])
    # ridge already tried, cloud is cooling down -> the ladder is exhausted and
    # the supervisor must escalate rather than re-run the same tier.
    assert verdict is None


def test_build_reroute_prompt_carries_the_failure_history():
    prompt = routing.build_reroute_prompt(
        "fix the failing test",
        [{"tier": "local", "reason": "gate failed", "evidence": "2 tests still red"}],
    )
    assert "fix the failing test" in prompt
    assert "gate failed" in prompt
    assert "2 tests still red" in prompt
    # No history = no note. A re-route prompt that invents context is worse than
    # the original.
    assert routing.build_reroute_prompt("x", []) == "x"


# ---------------------------------------------------------------------------
# outcomes — the label
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_self_report_is_not_evidence():
    """exit_code 0 + a done token + no diff is a FAILED session."""
    db = FakeDB(coding_sessions=[_session(
        result_summary="RALPH_DONE - all tests pass",
        created_at=NOW - timedelta(minutes=20),
    )])
    outcome = await OutcomeScorer(db).score_session("sess-1")
    assert outcome["success"] is False
    assert outcome["verified"] is True
    assert outcome["reason"] == "no diff produced"


@pytest.mark.asyncio
async def test_crash_reported_as_completed_is_a_failure():
    db = FakeDB(coding_sessions=[_session(
        created_at=NOW - timedelta(seconds=5), completed_at=NOW,
    )])
    outcome = await OutcomeScorer(db).score_session("sess-1")
    assert outcome["success"] is False
    assert "crash" in outcome["reason"]


@pytest.mark.asyncio
async def test_gate_and_diff_make_a_success():
    db = FakeDB(
        coding_sessions=[_session()],
        guard_gate_runs=[{
            "session_id": "sess-1", "passed": True, "at": NOW,
            "checks": [{"name": "diff_size", "passed": True, "files": 3, "lines": 42}],
        }],
    )
    outcome = await OutcomeScorer(db).score_session("sess-1")
    assert outcome["success"] is True
    assert outcome["gate_passed"] is True
    assert (outcome["diff_files"], outcome["diff_lines"]) == (3, 42)


@pytest.mark.asyncio
async def test_failed_gate_beats_a_large_diff():
    db = FakeDB(
        coding_sessions=[_session()],
        guard_gate_runs=[{
            "session_id": "sess-1", "passed": False, "at": NOW,
            "checks": [{"name": "diff_size", "passed": True, "files": 9, "lines": 400}],
        }],
    )
    outcome = await OutcomeScorer(db).score_session("sess-1")
    assert outcome["success"] is False
    assert outcome["reason"] == "C1 verification gate failed"


@pytest.mark.asyncio
async def test_merge_is_the_strongest_evidence_and_rollback_overrides_it():
    db = FakeDB(
        coding_sessions=[_session()],
        guard_events=[{"session_id": "sess-1", "kind": "merge:done", "blocked": False}],
    )
    outcome = await OutcomeScorer(db).score_session("sess-1")
    assert outcome["success"] is True and outcome["merged"] is True

    db2 = FakeDB(
        coding_sessions=[_session()],
        guard_events=[
            {"session_id": "sess-1", "kind": "merge:done", "blocked": False},
            {"session_id": "sess-1", "kind": "session:rollback", "blocked": False},
        ],
    )
    outcome2 = await OutcomeScorer(db2).score_session("sess-1")
    assert outcome2["success"] is False and outcome2["rolled_back"] is True


@pytest.mark.asyncio
async def test_independent_review_can_reject_a_gate_green_diff():
    db = FakeDB(
        coding_sessions=[_session()],
        session_reports=[{"session_id": "sess-1", "diff_numstat": "10\t2\tapi/x.py\n"}],
        session_reviews=[{
            "session_id": "sess-1", "ran": True, "blocking": True,
            "verdict": "reject", "summary": "removes the killswitch check",
            "independent": True,
        }],
    )
    outcome = await OutcomeScorer(db).score_session("sess-1")
    assert outcome["success"] is False
    assert "review rejected" in outcome["reason"]


@pytest.mark.asyncio
async def test_unverified_is_neither_success_nor_a_measured_failure():
    db = FakeDB(
        coding_sessions=[_session()],
        session_reports=[{
            "session_id": "sess-1",
            "diff_numstat": "10\t2\tapi/x.py\n",
            "tests": {"ran": False, "success": False},
        }],
    )
    outcome = await OutcomeScorer(db).score_session("sess-1")
    # None, not False: the improver and the weekly report both average this
    # field, and counting an unchecked session as a failure would make the
    # success rate a function of how many projects have a check_command.
    assert (outcome["success"], outcome["verified"]) == (None, False)
    assert outcome["reason"].startswith("unverified")


@pytest.mark.asyncio
async def test_running_sessions_are_not_scored_and_scoring_is_idempotent():
    db = FakeDB(coding_sessions=[_session(status="running")])
    scorer = OutcomeScorer(db)
    assert await scorer.score_session("sess-1") is None

    db2 = FakeDB(coding_sessions=[_session()])
    first = await OutcomeScorer(db2).score_session("sess-1")
    again = await OutcomeScorer(db2).score_session("sess-1")
    assert first["reason"] == again["reason"]
    assert len(db2.session_outcomes.docs) == 1


@pytest.mark.asyncio
async def test_score_pending_backfills_and_skips_scored_sessions():
    db = FakeDB(coding_sessions=[
        _session(_id="a"), _session(_id="b", status="stopped"),
        _session(_id="c", status="running"),
    ])
    scorer = OutcomeScorer(db)
    scored = await scorer.score_pending()
    assert {row["session_id"] for row in scored} == {"a", "b"}
    assert await scorer.score_pending() == []


@pytest.mark.asyncio
async def test_time_to_first_diff_comes_from_guard_checkpoints():
    db = FakeDB(
        coding_sessions=[_session()],
        guard_checkpoints=[
            {"session_id": "sess-1", "at": NOW - timedelta(minutes=25), "files": 0, "deletions": 0},
            {"session_id": "sess-1", "at": NOW - timedelta(minutes=20), "files": 2},
        ],
    )
    outcome = await OutcomeScorer(db).score_session("sess-1")
    # 30-minute session, first real checkpoint at +10 minutes.
    assert outcome["time_to_first_diff_seconds"] == 600


# ---------------------------------------------------------------------------
# outcomes — token attribution
# ---------------------------------------------------------------------------

def test_scan_pi_jsonl_sums_message_usage(tmp_path):
    path = tmp_path / "2026-08-12T23-46-31-940Z_sess-1.jsonl"
    path.write_text(
        json.dumps({"type": "session", "id": "x"}) + "\n"
        + json.dumps({"type": "message", "message": {
            "role": "assistant", "model": "DS4-0731", "provider": "ds4",
            "usage": {"input": 1525, "output": 23, "cacheRead": 10},
        }}) + "\n"
        + json.dumps({"type": "message", "message": {
            "role": "assistant", "model": "DS4-0731", "provider": "ds4",
            "usage": {"input": 2000, "output": 40},
        }}) + "\n"
        + "{ truncated line\n"  # a live session's last line is often partial
    )
    totals = scan_pi_jsonl(str(path))
    assert (totals["tokens_in"], totals["tokens_out"], totals["turns"]) == (3525, 63, 2)
    assert totals["model"] == "DS4-0731"


@pytest.mark.asyncio
async def test_unknown_token_counts_stay_none(tmp_path):
    db = FakeDB(coding_sessions=[_session()])
    with patch.object(outcomes_mod, "PI_SESSIONS_ROOT", str(tmp_path)):
        outcome = await OutcomeScorer(db).score_session("sess-1")
    # None, not 0 — a zero is indistinguishable from a measured zero and would
    # deflate cost-per-merged-change without anyone noticing.
    assert outcome["tokens_in"] is None
    assert outcome["cost_usd"] is None
    assert outcome["usage_source"] == "unavailable"


@pytest.mark.asyncio
async def test_pi_transcript_usage_is_mirrored_into_db_usage_once(tmp_path):
    session_dir = tmp_path / "--home-ben-Development-ProjectAria--"
    session_dir.mkdir()
    (session_dir / "2026-08-12T00-00-00-000Z_sess-1.jsonl").write_text(
        json.dumps({"type": "message", "message": {
            "model": "DS4-0731", "provider": "ds4",
            "usage": {"input": 1000, "output": 100},
        }}) + "\n"
    )
    db = FakeDB(coding_sessions=[_session()])
    with patch.object(outcomes_mod, "PI_SESSIONS_ROOT", str(tmp_path)):
        scorer = OutcomeScorer(db)
        outcome = await scorer.score_session("sess-1")
        await scorer.score_session("sess-1", force=True)

    assert (outcome["tokens_in"], outcome["tokens_out"]) == (1000, 100)
    assert outcome["cost_usd"] == 0.0  # local hardware is free, not UNKNOWN_CLOUD
    rows = [d for d in db.usage.docs if d["source"] == "coding:pi"]
    assert len(rows) == 1  # re-scoring must not double-count


@pytest.mark.asyncio
async def test_load_transcript_is_the_preferred_path():
    """The shared parser's real shape: PiTranscript.usage.to_dict() with pi's
    own key names (`input`/`output`), plus model/provider on the transcript."""
    class _Usage:
        def to_dict(self):
            return {"input": 4200, "output": 310, "cache_read": 900,
                    "cache_write": 0, "reasoning": 88, "total": 4510}

    transcript = SimpleNamespace(
        usage=_Usage(), model="DS4-0731-UD-IQ3-XXS-Halo", provider="ds4",
        turns=[object(), object()],
    )

    async def load_transcript(session_id, workspace=None, **kwargs):
        return transcript

    module = SimpleNamespace(load_transcript=load_transcript)
    with patch.object(outcomes_mod, "_load_pi_transcript_module", lambda: module):
        usage = await outcomes_mod.pi_usage("sess-1", workspace="/tmp/ws")

    assert (usage["tokens_in"], usage["tokens_out"]) == (4200, 310)
    assert usage["cache_read"] == 900
    assert usage["model"] == "DS4-0731-UD-IQ3-XXS-Halo"
    assert usage["backend"] == "ds4"
    assert usage["turns"] == 2
    assert usage["source"] == "pi_transcript.load_transcript"


@pytest.mark.asyncio
async def test_shared_pi_transcript_module_is_preferred_when_present():
    fake_module = SimpleNamespace(
        session_usage=lambda sid: {"input_tokens": 7, "output_tokens": 3, "model": "DS4"}
    )
    with patch.object(outcomes_mod, "_load_pi_transcript_module", lambda: fake_module):
        usage = await outcomes_mod.pi_usage("sess-1")
    assert usage["tokens_in"] == 7
    assert usage["source"] == "pi_transcript.session_usage"


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_cover_the_weekly_report_set():
    db = FakeDB(
        session_outcomes=[
            {"session_id": "a", "project_slug": "aria", "model": "claude-sonnet-5",
             "tier": "cloud", "success": True, "verified": True, "gate_passed": True,
             "nudges": 2, "rungs_used": 0, "merged": True, "tokens_in": 1000,
             "tokens_out": 200, "cost_usd": 0.006, "time_to_first_diff_seconds": 120,
             "created_at": NOW},
            {"session_id": "b", "project_slug": "aria", "model": "DS4-0731",
             "tier": "local", "success": False, "verified": True, "gate_passed": False,
             "nudges": 6, "rungs_used": 2, "rolled_back": True, "tokens_in": 5000,
             "tokens_out": 400, "cost_usd": 0.0, "time_to_first_diff_seconds": 900,
             "created_at": NOW},
            {"session_id": "c", "project_slug": "aria", "model": "DS4-0731",
             "tier": "local", "success": False, "verified": False, "nudges": 0,
             "rungs_used": 0, "created_at": NOW},
        ],
        alerts=[
            {"needs_human": True, "project_slug": "aria", "created_at": NOW,
             "decision": {"value": "IGNORE"}},
            {"needs_human": True, "project_slug": "aria", "created_at": NOW,
             "decision": None},
        ],
        guard_events=[
            {"kind": "merge:refused", "blocked": True, "at": NOW},
            {"kind": "policy:tamper", "blocked": True, "at": NOW},
        ],
    )
    report = await metrics(db, days=7, now=NOW)

    assert report["sessions"] == 3
    # The unverified row is excluded from the rate rather than counted as a
    # failure: 1 success out of 2 verified.
    assert report["success_rate"] == 0.5
    assert report["unverified_sessions"] == 1
    assert report["gate_pass_rate"] == 0.5
    assert report["nudges_per_success"] == 8.0
    assert report["stall_rate"] == round(1 / 3, 4)
    assert report["raises_per_day"] == round(2 / 7, 2)
    assert report["false_raise_rate"] == 0.5
    assert report["rollbacks"] == 1
    assert report["blocked_actions"] == 2
    assert report["tamper_events"] == 1
    assert report["merged_changes"] == 1
    assert report["cost_usd_per_merged_change"] == 0.006
    assert report["tokens_per_merged_change"] == 6600.0
    assert report["success_rate_by_tier"]["local"]["success_rate"] == 0.0
    assert report["usage_attribution_rate"] == round(2 / 3, 4)


@pytest.mark.asyncio
async def test_metrics_report_no_data_as_none_not_zero():
    report = await metrics(FakeDB(), days=7, now=NOW)
    # 0% success and "nothing ran yet" are different claims; conflating them
    # would read as a regression in the weekly report.
    assert report["success_rate"] is None
    assert report["gate_pass_rate"] is None
    assert report["cost_usd_per_merged_change"] is None
    assert report["sessions"] == 0


@pytest.mark.asyncio
async def test_worker_is_off_when_the_flag_is_off():
    db = FakeDB(coding_sessions=[_session()])
    worker = OutcomeWorker(db, interval_seconds=60)
    with patch.object(outcomes_mod.settings, "outcome_scoring_enabled", False):
        await worker.start()
    assert worker._task is None
    await worker.stop()


@pytest.mark.asyncio
async def test_worker_tick_scores_without_an_llm():
    db = FakeDB(coding_sessions=[_session()])
    worker = OutcomeWorker(db, interval_seconds=60)
    with patch.object(review_mod.CodingReviewService, "review_diff", AsyncMock()) as review:
        scored = await worker.tick()
    assert len(scored) == 1
    # The scorer is evidence-only: no model call, so it can run on a timer
    # without competing for a local slot.
    review.assert_not_called()
