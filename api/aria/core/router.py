"""
ARIA - Model Routing Policy

Purpose: Suggest a backend for a task by simple heuristics. This is ADVISORY
only — the user pins explicitly via `/model`, and an explicit pin always wins.
`/route <task>` applies a suggestion (which is just a pin you can override).
"""

from __future__ import annotations

# (keywords, backend, why). First match wins; order matters.
_RULES = [
    (("architect", "design", "complex", "tricky", "hard", "reason", "analyze", "plan"),
     "agentic", "heavy reasoning → local agentic model"),
    (("quick", "simple", "trivial", "summarize", "summary", "rename", "format", "typo", "tldr"),
     "llamacpp", "simple/cheap → local qwen-chat"),
    (("implement", "code", "refactor", "debug", "script", "tool", "agent", "build", "fix"),
     "agentic", "agentic/tool-use → qwen-agentic"),
]


def suggest_backend(hint: str) -> tuple[str, str]:
    """Return (backend, why) for a task hint."""
    h = (hint or "").lower()
    for keywords, backend, why in _RULES:
        if any(k in h for k in keywords):
            return backend, why
    return "llamacpp", "default"
