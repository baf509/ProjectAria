"""Tests for aria.research.service — JSON parsing, deduplication, tag extraction."""

import pytest

from aria.research.models import Learning
from aria.research.service import ResearchService

from tests.conftest import make_mock_db


@pytest.fixture
def research_service():
    db = make_mock_db()
    # Pass None for task_runner since we're only testing pure methods
    service = ResearchService.__new__(ResearchService)
    service.db = db
    return service


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

class TestParseJson:
    def test_plain_json_array(self, research_service):
        result = research_service._parse_json('["a", "b", "c"]')
        assert result == ["a", "b", "c"]

    def test_json_with_code_fence(self, research_service):
        result = research_service._parse_json('```json\n["a", "b"]\n```')
        assert result == ["a", "b"]

    def test_json_object_with_items_key(self, research_service):
        result = research_service._parse_json('{"items": [1, 2, 3]}')
        assert result == [1, 2, 3]

    def test_plain_object_without_items(self, research_service):
        result = research_service._parse_json('{"key": "value"}')
        assert result == []  # No 'items' key, returns empty

    def test_invalid_json_returns_empty(self, research_service):
        result = research_service._parse_json("not json at all")
        assert result == []

    def test_code_fence_without_language(self, research_service):
        result = research_service._parse_json('```\n["x"]\n```')
        assert result == ["x"]


# ---------------------------------------------------------------------------
# Learning deduplication
# ---------------------------------------------------------------------------

class TestDedupeLearnings:
    def test_removes_duplicates(self, research_service):
        learnings = [
            Learning(content="Python is great", source_url=None, confidence=0.9, depth_found=0, query_context="q"),
            Learning(content="python is great", source_url=None, confidence=0.8, depth_found=0, query_context="q"),
        ]
        deduped = research_service._dedupe_learnings(learnings)
        assert len(deduped) == 1
        # Should keep the first one
        assert deduped[0].confidence == 0.9

    def test_preserves_unique(self, research_service):
        learnings = [
            Learning(content="Fact A", source_url=None, confidence=0.9, depth_found=0, query_context="q"),
            Learning(content="Fact B", source_url=None, confidence=0.8, depth_found=0, query_context="q"),
        ]
        deduped = research_service._dedupe_learnings(learnings)
        assert len(deduped) == 2

    def test_empty_list(self, research_service):
        assert research_service._dedupe_learnings([]) == []

    def test_whitespace_variation(self, research_service):
        learnings = [
            Learning(content="  Hello world  ", source_url=None, confidence=0.9, depth_found=0, query_context="q"),
            Learning(content="hello world", source_url=None, confidence=0.8, depth_found=0, query_context="q"),
        ]
        deduped = research_service._dedupe_learnings(learnings)
        assert len(deduped) == 1


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_removes_tags(self, research_service):
        result = research_service._strip_html("<p>Hello <b>world</b></p>")
        assert result == "Hello world"

    def test_removes_scripts(self, research_service):
        result = research_service._strip_html("<script>alert('xss')</script>Content")
        assert "alert" not in result
        assert "Content" in result

    def test_removes_styles(self, research_service):
        result = research_service._strip_html("<style>.cls{color:red}</style>Text")
        assert "color" not in result
        assert "Text" in result

    def test_collapses_whitespace(self, research_service):
        result = research_service._strip_html("Hello    \n\n   world")
        assert result == "Hello world"

    def test_empty_string(self, research_service):
        assert research_service._strip_html("") == ""


# ---------------------------------------------------------------------------
# Query tag extraction
# ---------------------------------------------------------------------------

class TestQueryTags:
    def test_basic_extraction(self, research_service):
        tags = research_service._query_tags("best Python frameworks 2026")
        assert "python" in tags
        assert "best" in tags
        assert "frameworks" in tags
        assert "2026" in tags

    def test_filters_short_tokens(self, research_service):
        tags = research_service._query_tags("is AI ok")
        assert "is" not in tags
        assert "ok" not in tags

    def test_max_6_tags(self, research_service):
        tags = research_service._query_tags("one two three four five six seven eight nine ten")
        assert len(tags) <= 6

    def test_empty_query(self, research_service):
        assert research_service._query_tags("") == []


# ---------------------------------------------------------------------------
# Endpoint pinning (2026-08-18)
#
# The steward's ResearchPlanner refuses to launch an unattended run unless
# `start_research` accepts an `endpoint` (`_launch_allowed` ->
# `_start_research_supports("endpoint")`), because an unpinned llamacpp run
# resolves through ARIA's /llm/v1 auto-route to the LARGEST resident model --
# DS4, which is pi's single coding slot. These tests are that contract.
# ---------------------------------------------------------------------------

class TestEndpointPinning:
    def test_start_research_accepts_endpoint(self):
        """The planner's launch gate is a signature check. If this parameter is
        renamed or dropped, proactive research silently stops launching."""
        import inspect

        params = inspect.signature(ResearchService.start_research).parameters
        for name in ("endpoint", "force_local", "project_id", "topic_hash"):
            assert name in params, f"start_research lost the `{name}` parameter"

    @pytest.mark.asyncio
    async def test_build_config_carries_the_pin(self, research_service):
        config = await research_service._build_config(
            query="q",
            depth=1,
            breadth=2,
            model="qwen3.8-27b-rocmfp4-r9700",
            backend="llamacpp",
            conversation_id=None,
            endpoint="http://127.0.0.1:8080/v1",
            force_local=True,
            project_id="proj-1",
            topic_hash="abc123",
        )
        assert config.endpoint == "http://127.0.0.1:8080/v1"
        assert config.force_local is True
        assert config.project_id == "proj-1"
        assert config.topic_hash == "abc123"

    def test_endpoint_reaches_the_adapter_as_base_url(self):
        """A pin that never reaches get_adapter is not a pin."""
        import re
        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "aria" / "research" / "service.py"
        text = source.read_text()
        calls = re.findall(r"llm_manager\.get_adapter\([^)]*\)", text)
        assert calls, "no get_adapter calls found in research service"
        for call in calls:
            assert "base_url=config.endpoint" in call, f"unpinned adapter call: {call}"

    @pytest.mark.asyncio
    async def test_force_local_skips_the_cloud_runner(self, research_service, monkeypatch):
        """With force_local, completions must stay on the pinned local endpoint.
        A 'local research' loop that quietly spends the cloud subscription is
        not the loop that was asked for."""
        from aria.llm.base import Message
        import aria.research.service as service_module

        monkeypatch.setattr(service_module.settings, "use_claude_runner", True)
        monkeypatch.setattr(
            service_module.ClaudeRunner, "is_available", staticmethod(lambda: True)
        )

        def _fail(*args, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("ClaudeRunner was used despite force_local=True")

        monkeypatch.setattr(service_module.ClaudeRunner, "__init__", _fail)

        class _Adapter:
            async def complete(self, **kwargs):
                return "local answer", None, {}

        research_service.usage_repo = None
        content, usage = await research_service._complete(
            adapter=_Adapter(),
            messages=[Message(role="user", content="hi")],
            temperature=0.3,
            max_tokens=64,
            source="test",
            conversation_id=None,
            model="qwen",
            backend="llamacpp",
            force_local=True,
        )
        assert content == "local answer"
        assert usage == {}

    @pytest.mark.asyncio
    async def test_recovered_run_is_re_pinned(self, research_service):
        """A task recovered after a restart must not fall back to the
        auto-route -- that is the DS4 eviction the pin exists to prevent."""
        captured = {}

        async def _fake_run_research(research_id, config):
            captured["config"] = config
            return {}

        async def _fake_get_run(research_id):
            return {
                "_id": research_id,
                "query": "q",
                "depth": 1,
                "breadth": 2,
                "backend": "llamacpp",
                "model": "qwen3.8-27b-rocmfp4-r9700",
                "endpoint": "http://127.0.0.1:8080/v1",
                "force_local": True,
                "project_id": "proj-1",
                "topic_hash": "abc123",
            }

        research_service.get_run = _fake_get_run
        research_service._run_research = _fake_run_research
        await research_service._recover_research_task({"research_id": "r1"})

        config = captured["config"]
        assert config.endpoint == "http://127.0.0.1:8080/v1"
        assert config.force_local is True
