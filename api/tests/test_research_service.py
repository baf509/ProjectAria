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
