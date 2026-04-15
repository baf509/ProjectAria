"""
ARIA - Watched Shells Context Builder

Purpose: Build a compact context block summarizing recent activity across
watched shells, for injection into the orchestrator's system prompt.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from aria.config import settings
from aria.core.tokenizer import count_tokens
from aria.shells.service import ShellService

logger = logging.getLogger(__name__)


async def build_shell_context(
    shell_service: ShellService,
    *,
    max_tokens: Optional[int] = None,
    lookback_hours: Optional[float] = None,
    lines_per_shell: Optional[int] = None,
    model: str = "default",
) -> str:
    """Return a markdown context block for recent shell activity.

    Empty string when disabled or there's nothing to show.
    """
    if not settings.shells_include_in_chat_context:
        return ""

    max_tokens = int(max_tokens or settings.shells_context_max_tokens or 2000)
    lookback_hours = float(
        lookback_hours if lookback_hours is not None else settings.shells_context_lookback_hours
    )
    lines_per_shell = int(lines_per_shell or settings.shells_context_lines_per_shell or 20)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    try:
        shells = await shell_service.list_shells(status=["active", "idle"])
    except Exception as exc:
        logger.debug("shell context: list_shells failed: %s", exc)
        return ""

    def _aware(dt):
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    shells = [s for s in shells if _aware(s.last_activity_at) >= cutoff]
    shells.sort(key=lambda s: _aware(s.last_activity_at), reverse=True)
    if not shells:
        return ""

    sections: list[str] = []
    for shell in shells:
        try:
            events = await shell_service.tail(shell.name, lines=lines_per_shell)
        except Exception:
            continue
        if not events:
            continue
        lines = []
        for ev in events:
            prefix = "> " if ev.kind == "input" else ""
            text = ev.text_clean.strip()
            if not text:
                continue
            lines.append(f"{prefix}{text}")
        if not lines:
            continue
        header = f"### {shell.short_name or shell.name} ({shell.status})"
        body = "\n".join(lines)
        sections.append(f"{header}\n```\n{body}\n```")

    if not sections:
        return ""

    block = (
        "\n\n## Watched Shells (Recent Activity)\n\n"
        "These are tmux sessions the user is actively using. "
        "Reference them when the user asks about 'my shell', 'the coding agent', etc.\n\n"
        + "\n\n".join(sections)
    )

    # Trim to token budget by dropping oldest sections first.
    while sections and count_tokens(block, model=model) > max_tokens:
        sections.pop()
        if not sections:
            return ""
        block = (
            "\n\n## Watched Shells (Recent Activity)\n\n"
            + "\n\n".join(sections)
        )

    return block
