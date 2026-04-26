"""Tests for planning service helper functions (pure, no I/O)."""
from __future__ import annotations

from aria.planning.service import _content_hash, _normalize_title, _slugify


class TestNormalizeTitle:
    def test_lowercases(self):
        assert _normalize_title("Add Tests") == "add tests"

    def test_collapses_whitespace(self):
        assert _normalize_title("  Add   tests  for  ") == "add tests for"

    def test_strips_terminal_punctuation(self):
        assert _normalize_title("Add tests.") == "add tests"
        assert _normalize_title("Add tests?") == "add tests"
        assert _normalize_title("Add tests!") == "add tests"
        assert _normalize_title("Add tests,") == "add tests"


class TestContentHash:
    def test_same_for_equivalent_titles(self):
        # Differs only in case, whitespace, terminal punctuation
        a = _content_hash("Add tests for the resize endpoint!")
        b = _content_hash("add tests for the resize endpoint")
        c = _content_hash("  ADD  Tests for the Resize endpoint.  ")
        assert a == b == c

    def test_different_for_different_titles(self):
        assert _content_hash("Buy milk") != _content_hash("Buy bread")

    def test_returns_hex(self):
        h = _content_hash("anything")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestSlugify:
    def test_basic(self):
        assert _slugify("My Project") == "my-project"

    def test_strips_special_chars(self):
        assert _slugify("iOS App!") == "ios-app"
        assert _slugify("ARIA / Shells") == "aria-shells"

    def test_collapses_separators(self):
        assert _slugify("foo  bar  baz") == "foo-bar-baz"
        assert _slugify("foo___bar") == "foo-bar"

    def test_strips_leading_trailing_dashes(self):
        assert _slugify("---hello---") == "hello"

    def test_empty_falls_back(self):
        assert _slugify("!!!") == "project"
        assert _slugify("") == "project"
