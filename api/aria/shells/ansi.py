"""
ARIA - Watched Shells ANSI utilities

Purpose: Strip ANSI escape sequences, normalize terminal output for storage,
and match prompt patterns for idle detection.
"""

from __future__ import annotations

import re


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_BACKSPACE_RE = re.compile(r".\x08")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences and normalize whitespace.

    Keeps printable characters and newlines. Collapses carriage returns to
    line boundaries (terminal overwrite semantics) and drops bare \\r.
    """
    if not text:
        return ""
    text = _OSC_RE.sub("", text)
    text = _ANSI_RE.sub("", text)
    # Resolve backspace pairs (rare in captured output but harmless)
    prev = None
    while prev != text:
        prev = text
        text = _BACKSPACE_RE.sub("", text)
    # Normalize CRLF → LF, drop bare CR
    text = text.replace("\r\n", "\n").replace("\r", "")
    return text


def matches_prompt(line: str, patterns: list[str]) -> bool:
    """Return True if `line` matches any of the idle prompt regex patterns."""
    line = (line or "").rstrip()
    for pattern in patterns:
        try:
            if re.search(pattern, line):
                return True
        except re.error:
            continue
    return False


def parse_prompt_patterns(raw: str | list[str]) -> list[str]:
    """Parse `SHELLS_IDLE_PROMPT_PATTERNS` config into a regex list.

    Accepts either a comma-separated string or a list.
    """
    if isinstance(raw, list):
        return [p for p in raw if p]
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]
