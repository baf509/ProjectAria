"""Tests for aria.shells.ansi."""
import pytest

from aria.shells.ansi import strip_ansi, matches_prompt, parse_prompt_patterns


class TestStripAnsi:
    def test_empty(self):
        assert strip_ansi("") == ""

    def test_plain(self):
        assert strip_ansi("hello world") == "hello world"

    def test_csi_color(self):
        assert strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_csi_cursor(self):
        assert strip_ansi("before\x1b[2Kafter") == "beforeafter"

    def test_osc(self):
        assert strip_ansi("\x1b]0;title\x07hi") == "hi"

    def test_crlf_normalized(self):
        assert strip_ansi("line1\r\nline2") == "line1\nline2"

    def test_bare_cr_dropped(self):
        assert strip_ansi("redraw\rfinal") == "redrawfinal"

    def test_backspace_removed(self):
        assert strip_ansi("abc\x08d") == "abd"


class TestMatchesPrompt:
    def test_question_mark(self):
        assert matches_prompt("Proceed?", [r"\?\s*$"]) is True

    def test_repl_prompt(self):
        assert matches_prompt(">>> ", [r">\s*$"]) is True

    def test_claude_human(self):
        assert matches_prompt("Human: ", [r"Human:\s*$"]) is True

    def test_no_match(self):
        assert matches_prompt("Running tests...", [r"\?\s*$"]) is False

    def test_bad_regex_ignored(self):
        assert matches_prompt("hi", ["[invalid"]) is False


class TestParsePromptPatterns:
    def test_list(self):
        assert parse_prompt_patterns(["a", "b"]) == ["a", "b"]

    def test_comma_string(self):
        assert parse_prompt_patterns("a, b , c") == ["a", "b", "c"]

    def test_empty(self):
        assert parse_prompt_patterns("") == []
