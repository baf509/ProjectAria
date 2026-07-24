"""
ARIA - Tests for coding-task complexity routing

Covers the three stages (heuristic → judge → availability), the judge's JSON
parsing tolerances, the inline-answer path, the verdict cache, and the
`start_session` integration that decides whether routing runs at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.agents.routing import (
    CLAUDE_PROVIDER,
    TIER_DEEP,
    TIER_FALLBACK,
    TIER_LIGHT,
    TIER_STANDARD,
    ComplexityRouter,
    RoutingVerdict,
    clear_cooldown,
    get_cooldown,
    record_quota_exhaustion,
)


@pytest.fixture(autouse=True)
def _clear_router_cache():
    """The verdict cache is process-wide; keep tests independent."""
    ComplexityRouter.clear_cache()
    yield
    ComplexityRouter.clear_cache()


def _judging_router(reply: str, db=None, transport: str | None = "api") -> ComplexityRouter:
    """A router whose judge transports return `reply` verbatim.

    `transport` pins the choice so judge-behaviour tests don't depend on what
    happens to be configured on the host; pass None to exercise the real
    `auto` resolution.
    """
    router = ComplexityRouter(db)
    router._judge_via_api = AsyncMock(return_value=reply)
    router._judge_via_cli = AsyncMock(return_value=reply)
    if transport is not None:
        router._resolve_transport = lambda: transport
    return router


# ---------------------------------------------------------------------------
# Stage 1 — heuristic prefilter
# ---------------------------------------------------------------------------

class TestHeuristic:
    @pytest.mark.parametrize(
        "prompt",
        [
            "design the architecture for the new fleet service",
            "Let's plan how to split shells out of the API",
            "what are the trade-offs between mongot and Atlas here",
            "strategize the migration off the legacy llamacpp endpoint",
        ],
    )
    def test_planning_language_is_deep(self, prompt):
        verdict = ComplexityRouter()._heuristic(prompt)
        assert verdict is not None
        assert verdict.tier == TIER_DEEP

    @pytest.mark.parametrize(
        "prompt",
        [
            "what is the connection string for mongot",
            "look up how the adopt worker reconciles shells",
            "summarize the last 50 lines of the changelog",
        ],
    )
    def test_research_language_is_light(self, prompt):
        verdict = ComplexityRouter()._heuristic(prompt)
        assert verdict is not None
        assert verdict.tier == TIER_LIGHT

    @pytest.mark.parametrize(
        "prompt",
        [
            "fix the failing test in test_nodes.py",
            "add a test for the prune worker",
            "rename the qwen-chat container",
        ],
    )
    def test_implementation_language_is_standard(self, prompt):
        verdict = ComplexityRouter()._heuristic(prompt)
        assert verdict is not None
        assert verdict.tier == TIER_STANDARD

    def test_ambiguous_prompt_falls_through_to_judge(self):
        assert ComplexityRouter()._heuristic("make the reaper better somehow") is None

    def test_deep_wins_over_standard_when_both_match(self):
        # "design ... refactor" — the structural call is the expensive one.
        verdict = ComplexityRouter()._heuristic("design a plan to fix the reaper")
        assert verdict.tier == TIER_DEEP

    def test_heuristic_maps_tier_to_configured_model(self):
        with patch("aria.agents.routing.settings") as s:
            s.coding_routing_model_deep = "claude-opus-4-8"
            s.coding_routing_model_standard = "claude-sonnet-5"
            s.coding_routing_model_light = "claude-sonnet-5"
            verdict = ComplexityRouter()._heuristic("design the new API surface")
        assert verdict.model == "claude-opus-4-8"
        assert verdict.backend == "claude_code"
        assert verdict.source == "heuristic"


# ---------------------------------------------------------------------------
# Stage 2 — judge parsing
# ---------------------------------------------------------------------------

class TestJudgeParsing:
    def test_plain_json(self):
        parsed = ComplexityRouter._parse_judge(
            '{"tier": "deep", "why": "architecture call", "answer": null}'
        )
        assert parsed == ("deep", "architecture call", None)

    def test_fenced_json(self):
        parsed = ComplexityRouter._parse_judge(
            '```json\n{"tier": "light", "why": "lookup", "answer": "8200"}\n```'
        )
        assert parsed == ("light", "lookup", "8200")

    def test_json_wrapped_in_prose(self):
        parsed = ComplexityRouter._parse_judge(
            'Sure! {"tier": "standard", "why": "scoped fix"} — hope that helps.'
        )
        assert parsed == ("standard", "scoped fix", None)

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not json at all",
            '{"tier": "extreme", "why": "nope"}',   # tier outside the enum
            '{"why": "missing tier"}',
            '["deep"]',                             # right word, wrong shape
            "{unclosed",
        ],
    )
    def test_unparseable_returns_none(self, raw):
        assert ComplexityRouter._parse_judge(raw) is None

    def test_non_string_answer_is_dropped(self):
        parsed = ComplexityRouter._parse_judge(
            '{"tier": "light", "why": "x", "answer": {"oops": 1}}'
        )
        assert parsed == ("light", "x", None)

    def test_why_is_truncated(self):
        parsed = ComplexityRouter._parse_judge(
            '{"tier": "deep", "why": "%s"}' % ("y" * 500)
        )
        assert len(parsed[1]) == 120


# ---------------------------------------------------------------------------
# Stage 2 — judge behaviour
# ---------------------------------------------------------------------------

class TestJudge:
    @pytest.mark.asyncio
    async def test_judge_result_used_when_heuristic_misses(self):
        router = _judging_router('{"tier": "deep", "why": "structural"}')
        verdict = await router.classify("make the reaper better somehow")
        assert verdict.tier == TIER_DEEP
        assert verdict.source == "judge"
        assert verdict.confidence == 0.9

    @pytest.mark.asyncio
    async def test_judge_failure_degrades_to_standard(self):
        router = ComplexityRouter()
        router._judge_via_api = AsyncMock(side_effect=RuntimeError("api down"))
        router._judge_via_cli = AsyncMock(side_effect=RuntimeError("cli down"))
        verdict = await router.classify("make the reaper better somehow")
        assert verdict.tier == TIER_STANDARD
        assert verdict.source == "default"
        assert verdict.confidence == 0.0

    @pytest.mark.asyncio
    async def test_unparseable_judge_degrades_to_standard(self):
        router = _judging_router("I think this is a hard one, honestly")
        verdict = await router.classify("make the reaper better somehow")
        assert verdict.tier == TIER_STANDARD
        assert verdict.source == "default"

    @pytest.mark.asyncio
    async def test_empty_prompt_short_circuits(self):
        router = ComplexityRouter()
        router._judge_via_api = AsyncMock(side_effect=AssertionError("must not call"))
        verdict = await router.classify("   ")
        assert verdict.tier == TIER_STANDARD


class TestTransportResolution:
    """`auto` must not pick the API path on a box with no Anthropic key —
    that's this machine, and it would fail every ambiguous classification."""

    @pytest.mark.parametrize(
        "configured,key,expected",
        [
            ("api", "", "api"),          # explicit wins even without a key
            ("cli", "sk-ant-x", "cli"),  # explicit wins even with one
            ("auto", "sk-ant-x", "api"),
            ("auto", "", "cli"),
            ("", "", "cli"),             # unset behaves as auto
            ("AUTO", "sk-ant-x", "api"), # case-insensitive
            ("nonsense", "", "cli"),     # unknown value degrades to auto
        ],
    )
    def test_resolution(self, configured, key, expected):
        with patch("aria.agents.routing.settings") as s:
            s.coding_routing_judge_transport = configured
            s.anthropic_api_key = key
            assert ComplexityRouter._resolve_transport() == expected

    @pytest.mark.asyncio
    async def test_auto_without_key_uses_cli_end_to_end(self):
        router = _judging_router('{"tier": "deep", "why": "x"}', transport=None)
        with patch("aria.agents.routing.settings") as s:
            s.coding_routing_judge_transport = "auto"
            s.anthropic_api_key = ""
            s.coding_routing_cache_ttl_seconds = 0
            s.coding_routing_model_deep = "claude-opus-4-8"
            s.coding_routing_model_standard = "claude-sonnet-5"
            s.coding_routing_model_light = "claude-sonnet-5"
            s.coding_routing_judge_model = "claude-sonnet-5"
            verdict = await router.classify("make the reaper better somehow")
        assert verdict.tier == TIER_DEEP
        router._judge_via_cli.assert_awaited_once()
        router._judge_via_api.assert_not_awaited()


class TestInlineAnswer:
    @pytest.mark.asyncio
    async def test_light_task_answered_inline_when_allowed(self):
        router = _judging_router(
            '{"tier": "light", "why": "general knowledge", "answer": "Port 8200."}'
        )
        verdict = await router.classify("remind me of the port", allow_inline_answer=True)
        assert verdict.tier == TIER_LIGHT
        assert verdict.answer == "Port 8200."

    @pytest.mark.asyncio
    async def test_answer_suppressed_when_not_allowed(self):
        router = _judging_router(
            '{"tier": "light", "why": "general knowledge", "answer": "Port 8200."}'
        )
        verdict = await router.classify("remind me of the port", allow_inline_answer=False)
        assert verdict.answer is None

    @pytest.mark.asyncio
    async def test_answer_ignored_on_non_light_tier(self):
        router = _judging_router(
            '{"tier": "deep", "why": "structural", "answer": "just do X"}'
        )
        verdict = await router.classify("something ambiguous here", allow_inline_answer=True)
        assert verdict.tier == TIER_DEEP
        assert verdict.answer is None

    @pytest.mark.asyncio
    async def test_inline_and_session_variants_do_not_share_a_cache_entry(self):
        router = _judging_router(
            '{"tier": "light", "why": "lookup", "answer": "42"}'
        )
        with_answer = await router.classify("ambiguous thing", allow_inline_answer=True)
        without = await router.classify("ambiguous thing", allow_inline_answer=False)
        assert with_answer.answer == "42"
        assert without.answer is None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class TestCache:
    @pytest.mark.asyncio
    async def test_repeat_classification_hits_cache(self):
        router = _judging_router('{"tier": "deep", "why": "structural"}')
        await router.classify("make the reaper better somehow")
        await router.classify("make the reaper better somehow")
        router._judge_via_api.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_zero_ttl_disables_cache(self):
        router = _judging_router('{"tier": "deep", "why": "structural"}')
        with patch("aria.agents.routing.settings") as s:
            s.coding_routing_judge_transport = "api"
            s.coding_routing_cache_ttl_seconds = 0
            s.coding_routing_model_deep = "claude-opus-4-8"
            s.coding_routing_model_standard = "claude-sonnet-5"
            s.coding_routing_model_light = "claude-sonnet-5"
            s.coding_routing_judge_model = "claude-sonnet-5"
            await router.classify("make the reaper better somehow")
            await router.classify("make the reaper better somehow")
        assert router._judge_via_api.await_count == 2


# ---------------------------------------------------------------------------
# Stage 3 — availability / quota cooldown
# ---------------------------------------------------------------------------

def _db_with_availability(doc):
    db = MagicMock()
    db.model_availability = MagicMock()
    db.model_availability.find_one = AsyncMock(return_value=doc)
    db.model_availability.update_one = AsyncMock()
    db.model_availability.delete_one = AsyncMock()
    return db


class TestAvailability:
    @pytest.mark.asyncio
    async def test_active_cooldown_demotes_to_fallback(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=30)
        db = _db_with_availability({"_id": CLAUDE_PROVIDER, "cooled_until": future})
        router = ComplexityRouter(db)
        verdict = await router.classify("design the new fleet API")
        assert verdict.tier == TIER_FALLBACK
        assert verdict.source == "fallback"
        assert verdict.backend != "claude_code"
        assert verdict.llm  # fallback carries an LLM backend for pi-code
        assert "quota" in verdict.why

    @pytest.mark.asyncio
    async def test_expired_cooldown_is_ignored(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        db = _db_with_availability({"_id": CLAUDE_PROVIDER, "cooled_until": past})
        verdict = await ComplexityRouter(db).classify("design the new fleet API")
        assert verdict.tier == TIER_DEEP

    @pytest.mark.asyncio
    async def test_no_db_skips_availability(self):
        verdict = await ComplexityRouter(None).classify("design the new fleet API")
        assert verdict.tier == TIER_DEEP

    @pytest.mark.asyncio
    async def test_availability_error_does_not_block(self):
        db = MagicMock()
        db.model_availability = MagicMock()
        db.model_availability.find_one = AsyncMock(side_effect=RuntimeError("mongo down"))
        verdict = await ComplexityRouter(db).classify("design the new fleet API")
        assert verdict.tier == TIER_DEEP

    @pytest.mark.asyncio
    async def test_naive_datetime_from_mongo_is_treated_as_utc(self):
        naive_future = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(tzinfo=None)
        db = _db_with_availability({"_id": CLAUDE_PROVIDER, "cooled_until": naive_future})
        assert await get_cooldown(db) is not None

    @pytest.mark.asyncio
    async def test_missing_row_means_available(self):
        assert await get_cooldown(_db_with_availability(None)) is None

    @pytest.mark.asyncio
    async def test_record_and_clear_cooldown(self):
        db = _db_with_availability(None)
        cooled_until = await record_quota_exhaustion(db, minutes=15, reason="test")
        assert cooled_until > datetime.now(timezone.utc)
        db.model_availability.update_one.assert_awaited_once()
        await clear_cooldown(db)
        db.model_availability.delete_one.assert_awaited_once()


# ---------------------------------------------------------------------------
# Verdict serialization
# ---------------------------------------------------------------------------

class TestVerdict:
    def test_to_meta_carries_the_explanation(self):
        meta = RoutingVerdict(
            tier=TIER_DEEP, backend="claude_code", model="claude-opus-4-8",
            why="architecture", confidence=0.9, source="judge",
            judge_model="claude-sonnet-5",
        ).to_meta()
        assert meta["tier"] == TIER_DEEP
        assert meta["why"] == "architecture"
        assert meta["judge_model"] == "claude-sonnet-5"
        assert isinstance(meta["decided_at"], datetime)
        # The model itself lives on the session doc; meta explains the choice.
        assert "model" not in meta

    def test_to_dict_is_the_wire_shape(self):
        d = RoutingVerdict(
            tier=TIER_LIGHT, backend="claude_code", model="claude-sonnet-5",
            why="lookup", answer="42",
        ).to_dict()
        assert d["answer"] == "42"
        assert set(d) == {
            "tier", "backend", "model", "llm", "why",
            "confidence", "source", "answer", "judge_model",
        }


# ---------------------------------------------------------------------------
# Backend guard env
# ---------------------------------------------------------------------------

class TestManagedEnv:
    def test_claude_code_marks_launches_as_aria_managed(self):
        from aria.agents.backends.base import StartParams
        from aria.agents.backends.claude_code import ClaudeCodeBackend

        params = StartParams(workspace="/tmp/ws", prompt="do it", model="claude-opus-4-8")
        backend = ClaudeCodeBackend()
        # Without this the desk-path wrapper would re-enter ARIA recursively.
        assert backend.start_command(params).env.get("ARIA_MANAGED") == "1"
        assert backend.resume_command("sid", params).env.get("ARIA_MANAGED") == "1"

    def test_codex_marks_launches_as_aria_managed(self):
        from aria.agents.backends.base import StartParams
        from aria.agents.backends.codex import CodexBackend

        params = StartParams(workspace="/tmp/ws", prompt="do it")
        assert CodexBackend().start_command(params).env.get("ARIA_MANAGED") == "1"

    def test_model_is_passed_through_to_the_cli(self):
        from aria.agents.backends.base import StartParams
        from aria.agents.backends.claude_code import ClaudeCodeBackend

        argv = ClaudeCodeBackend().start_command(
            StartParams(workspace="/tmp/ws", prompt="do it", model="claude-opus-4-8")
        ).argv
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# start_session integration — when does routing run at all?
# ---------------------------------------------------------------------------

def _routing_manager():
    """A CodingSessionManager stubbed down to the routing decision point."""
    from tests.conftest import make_mock_db

    with patch("aria.agents.session.TmuxManager") as TmuxCls, \
         patch("aria.agents.session.BackendRegistry"), \
         patch("aria.agents.session.CodingSubprocessManager"), \
         patch("aria.agents.session.AgentMailbox"), \
         patch("aria.agents.session.ShellService"):
        TmuxCls.is_available.return_value = False
        from aria.agents.session import CodingSessionManager

        mgr = CodingSessionManager(make_mock_db())

    backend = MagicMock()
    backend.is_in_process = False
    backend.start_command.return_value = MagicMock(argv=["claude"], cwd="/tmp/ws", env={})
    mgr.registry = MagicMock()
    mgr.registry.get.return_value = backend
    mgr.shell_service = None
    mgr.process_manager = MagicMock()
    running = MagicMock()
    running.process.pid = 4242
    mgr.process_manager.spawn = AsyncMock(return_value=running)
    mgr._watch_session = AsyncMock()
    mgr.get_session = AsyncMock(return_value={"_id": "sid"})
    return mgr


@pytest.fixture
def _no_safety_gates():
    """start_session consults the killswitch and e-stop before spawning."""
    estop = MagicMock()
    estop.is_active = AsyncMock(return_value=False)
    with patch("aria.api.deps.get_killswitch") as ks, \
         patch("aria.api.deps.resolve_estop_manager", AsyncMock(return_value=estop)):
        ks.return_value.check_or_raise = MagicMock()
        yield


class TestStartSessionRouting:
    @pytest.mark.asyncio
    async def test_unpinned_session_is_routed(self, _no_safety_gates):
        mgr = _routing_manager()
        verdict = RoutingVerdict(
            tier=TIER_DEEP, backend="claude_code", model="claude-opus-4-8",
            why="architecture", confidence=0.9, source="judge",
        )
        with patch("aria.agents.routing.ComplexityRouter") as RouterCls:
            RouterCls.return_value.classify = AsyncMock(return_value=verdict)
            await mgr.start_session(workspace="/tmp/ws", backend=None, prompt="design it")

        doc = mgr.db.coding_sessions.insert_one.call_args[0][0]
        assert doc["model"] == "claude-opus-4-8"
        assert doc["backend"] == "claude_code"
        assert doc["routing"]["tier"] == TIER_DEEP
        assert doc["routing"]["why"] == "architecture"

    @pytest.mark.asyncio
    async def test_explicit_model_is_never_overridden(self, _no_safety_gates):
        mgr = _routing_manager()
        with patch("aria.agents.routing.ComplexityRouter") as RouterCls:
            RouterCls.return_value.classify = AsyncMock(
                side_effect=AssertionError("router must not run on a pinned model")
            )
            await mgr.start_session(
                workspace="/tmp/ws", backend=None, prompt="design it",
                model="claude-haiku-4-5",
            )

        doc = mgr.db.coding_sessions.insert_one.call_args[0][0]
        assert doc["model"] == "claude-haiku-4-5"
        assert doc["routing"] is None

    @pytest.mark.asyncio
    async def test_explicit_backend_is_never_overridden(self, _no_safety_gates):
        mgr = _routing_manager()
        with patch("aria.agents.routing.ComplexityRouter") as RouterCls:
            RouterCls.return_value.classify = AsyncMock(
                side_effect=AssertionError("router must not run on a pinned backend")
            )
            await mgr.start_session(
                workspace="/tmp/ws", backend="codex", prompt="design it",
            )

        assert mgr.db.coding_sessions.insert_one.call_args[0][0]["backend"] == "codex"

    @pytest.mark.asyncio
    async def test_explicit_claude_backend_still_routes(self, _no_safety_gates):
        """Hermes passes backend="claude_code" as belt-and-suspenders. That is
        the backend the router picks anyway, so it must not suppress routing —
        it did, which left every Hermes task on the default model."""
        mgr = _routing_manager()
        verdict = RoutingVerdict(
            tier=TIER_DEEP, backend="claude_code", model="claude-opus-4-8",
            why="architecture", confidence=0.9, source="judge",
        )
        with patch("aria.agents.routing.ComplexityRouter") as RouterCls:
            RouterCls.return_value.classify = AsyncMock(return_value=verdict)
            await mgr.start_session(
                workspace="/tmp/ws", backend="claude_code", prompt="design it",
            )

        doc = mgr.db.coding_sessions.insert_one.call_args[0][0]
        assert doc["model"] == "claude-opus-4-8"
        assert doc["routing"]["tier"] == TIER_DEEP

    def test_is_routable_backend(self):
        from aria.agents.routing import is_routable_backend

        assert is_routable_backend(None) is True
        assert is_routable_backend("claude_code") is True
        assert is_routable_backend("codex") is False
        assert is_routable_backend("pi-code") is False

    @pytest.mark.asyncio
    async def test_routing_failure_falls_through_to_defaults(self, _no_safety_gates):
        mgr = _routing_manager()
        with patch("aria.agents.routing.ComplexityRouter") as RouterCls:
            RouterCls.return_value.classify = AsyncMock(side_effect=RuntimeError("boom"))
            # Must not raise: a router problem can't stop you starting work.
            await mgr.start_session(workspace="/tmp/ws", backend=None, prompt="design it")

        doc = mgr.db.coding_sessions.insert_one.call_args[0][0]
        assert doc["routing"] is None
        assert doc["model"] is None

    @pytest.mark.asyncio
    async def test_routing_can_be_disabled(self, _no_safety_gates):
        mgr = _routing_manager()
        with patch("aria.agents.session.settings") as s, \
             patch("aria.agents.routing.ComplexityRouter") as RouterCls:
            s.coding_routing_enabled = False
            s.coding_default_backend = "codex"
            RouterCls.return_value.classify = AsyncMock(
                side_effect=AssertionError("router must not run when disabled")
            )
            await mgr.start_session(workspace="/tmp/ws", backend=None, prompt="design it")

        assert mgr.db.coding_sessions.insert_one.call_args[0][0]["routing"] is None
