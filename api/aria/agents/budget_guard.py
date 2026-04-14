"""
ARIA - Context Budget Guard

Purpose: Monitor coding sessions for context window exhaustion and
trigger checkpoint + handoff when budget is critically low.

Inspired by Gas Town's context-budget-guard.sh which monitors token
usage and enforces handoff thresholds.

Since ARIA doesn't have direct access to the agent's token counts,
we use heuristic signals from agent output:
- Explicit context limit messages from the LLM provider
- Conversation compaction/truncation messages from Claude Code
- Increasing response latency (context processing overhead)
- Output that mentions running out of context

Thresholds:
- WARN (75%):  Log warning, no action
- SOFT (85%):  Write checkpoint, notify user
- HARD (92%):  Write checkpoint, stop session, suggest resume
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class BudgetLevel(str, Enum):
    OK = "ok"
    WARN = "warn"
    SOFT_GATE = "soft_gate"
    HARD_GATE = "hard_gate"


# Patterns indicating various levels of context pressure
_WARN_PATTERNS = [
    re.compile(r"context.?(is )?(getting|becoming)\s+(long|large)", re.IGNORECASE),
    re.compile(r"conversation.?(is )?(getting|becoming)\s+long", re.IGNORECASE),
    re.compile(r"running low on.?context", re.IGNORECASE),
]

_SOFT_PATTERNS = [
    re.compile(r"(compact|truncat|compress)(ing|ed)\s+(context|conversation|messages)", re.IGNORECASE),
    re.compile(r"(dropped|removed|trimmed)\s+\d+\s+(old|earlier|previous)\s+messages", re.IGNORECASE),
    re.compile(r"context.?(window|limit).?(near|approach|close to)", re.IGNORECASE),
    re.compile(r"summariz(ing|ed)\s+(earlier|previous|old)\s+(messages|conversation)", re.IGNORECASE),
]

_HARD_PATTERNS = [
    re.compile(r"(context|token).?(window|limit)\s*(exceeded|full|exhausted|reached)", re.IGNORECASE),
    re.compile(r"(maximum|max)\s*(context|token)\s*(length|limit|count)\s*(exceeded|reached)", re.IGNORECASE),
    re.compile(r"input.?too.?(long|large)", re.IGNORECASE),
    re.compile(r"(must|need to)\s*(reduce|shorten|truncate)", re.IGNORECASE),
    re.compile(r"request.?too.?large", re.IGNORECASE),
]


def assess_budget(output: str) -> BudgetLevel:
    """Assess context budget level from agent output.

    Checks the last ~50 lines of output for context pressure signals.
    Returns the highest severity level detected.
    """
    if not output:
        return BudgetLevel.OK

    tail = "\n".join(output.splitlines()[-50:])

    # Check from most severe to least
    for pattern in _HARD_PATTERNS:
        if pattern.search(tail):
            return BudgetLevel.HARD_GATE

    for pattern in _SOFT_PATTERNS:
        if pattern.search(tail):
            return BudgetLevel.SOFT_GATE

    for pattern in _WARN_PATTERNS:
        if pattern.search(tail):
            return BudgetLevel.WARN

    return BudgetLevel.OK


class ContextBudgetGuard:
    """Monitor coding sessions for context budget exhaustion.

    Designed to be called from the CodingWatchdog during its check loop.
    """

    def __init__(self):
        # Track per-session budget state to avoid duplicate notifications
        self._session_levels: dict[str, BudgetLevel] = {}

    def check(self, session_id: str, output: str) -> Optional[BudgetLevel]:
        """Check a session's output for budget pressure.

        Returns the new BudgetLevel only if it escalated since the last check.
        Returns None if the level is unchanged or lower.
        Also tracks the current level so de-escalation is possible if the
        output window rolls past the pressure signals.
        """
        level = assess_budget(output)
        prev = self._session_levels.get(session_id, BudgetLevel.OK)

        # Always update to current level (allows de-escalation tracking)
        self._session_levels[session_id] = level

        severity_order = [BudgetLevel.OK, BudgetLevel.WARN, BudgetLevel.SOFT_GATE, BudgetLevel.HARD_GATE]
        if severity_order.index(level) > severity_order.index(prev):
            return level

        return None

    def clear(self, session_id: str) -> None:
        """Clear tracking for a completed session."""
        self._session_levels.pop(session_id, None)
