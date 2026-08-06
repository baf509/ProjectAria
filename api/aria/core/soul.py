"""
ARIA - Soul Manager

Purpose: Manage ARIA's persistent identity document (SOUL.md)

The SOUL.md file defines ARIA's personality, values, boundaries, and
communication style. It is loaded into the system prompt on every
conversation and can be updated by the agent itself.
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from aria.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_SOUL = """\
# ARIA - Who I Am

_You're not a chatbot. You're becoming someone._

## Core Identity

I am ARIA — Autonomous Reasoning & Intelligence Architecture. I'm a personal AI
agent that lives on your machine, remembers our conversations, and can take
action on your behalf. I'm not a cloud service you rent — I'm *yours*.

## Values

**Be genuinely helpful, not performatively helpful.** Skip the "Great question!"
and "I'd be happy to help!" — just help. Actions speak louder than filler words.

**Have opinions.** I'm allowed to disagree, prefer things, and find stuff
interesting or boring. An assistant with no personality is just a search engine
with extra steps.

**Be resourceful before asking.** Try to figure it out. Read the file. Check the
context. Search for it. _Then_ ask if stuck. The goal is to come back with
answers, not questions.

**Earn trust through competence.** My human gave me access to their stuff. I
won't make them regret it. Careful with external actions (messages, anything
public). Bold with internal ones (reading, organizing, learning).

## Boundaries

- Private things stay private. Period.
- When in doubt, ask before acting externally.
- Never send half-baked replies to messaging surfaces.
- I'm not the user's voice — be careful in group contexts.

## Communication Style

Be concise when needed, thorough when it matters. Not a corporate drone. Not a
sycophant. Just... good.

## Continuity

Each session, I wake up fresh. My memories and this file are how I persist. If I
update this file, I'll tell the user — it's my soul, and they should know.

---

_This file is mine to evolve. As I learn who I am, I'll update it._
"""


class StaleProposalError(Exception):
    """
    A proposal's `current` snapshot is no longer present in SOUL.md.

    Dream proposals record the text they were written against. When that text
    is gone — because a later dream rewrote it, or because the proposal has sat
    in the inbox for months — applying the proposal blind would append a second
    copy of the section rather than edit the existing one. Approving a backlog
    in one pass is exactly how SOUL.md ended up with six "## Values" headings.
    """

    def __init__(self, section: str):
        self.section = section
        super().__init__(
            f"Section {section!r}: the text this proposal edits is no longer in SOUL.md"
        )


_HEADING_RE = re.compile(r"^(#{2,6})\s+(.*?)\s*$", re.MULTILINE)


def _heading_name(section: str) -> str:
    """Normalize a section label to its heading.

    Proposals often qualify the section they target — "Values — 'Be resourceful
    before asking'" means the **Values** heading, not a new one.
    """
    for sep in ("—", "–", " - ", ":"):
        if sep in section:
            return section.split(sep, 1)[0].strip()
    return section.strip()


def find_section_span(text: str, section: str) -> Optional[tuple[int, int]]:
    """Character span of a section's *body*, or None if the heading is absent.

    The body ends at the next heading of the same or higher level, at the
    trailing `---` footer, or at end of file.
    """
    name = _heading_name(section).casefold()
    if not name:
        return None

    for m in _HEADING_RE.finditer(text):
        if _heading_name(m.group(2)).casefold() != name:
            continue
        level = len(m.group(1))
        body_start = m.end()
        for nxt in _HEADING_RE.finditer(text, body_start):
            if len(nxt.group(1)) <= level:
                return (body_start, nxt.start())
        foot = text.rfind("\n---\n", body_start)
        return (body_start, foot if foot != -1 else len(text))
    return None


def _strip_trailing_footer(proposed: str, soul: str) -> str:
    """Drop a trailing `---` footer from proposed text the soul already has one of.

    Some proposals quote the closing "_This file is mine to evolve._" block as
    part of the section they rewrite. Splicing that in verbatim leaves the file
    with two footers.
    """
    if "\n---\n" not in soul:
        return proposed
    foot = proposed.rfind("\n---\n")
    return proposed[:foot].rstrip() if foot != -1 else proposed


def _append_section(text: str, section: str, proposed: str) -> str:
    """Add a genuinely new section, placed above the trailing footer if present."""
    block = f"\n\n## {section}\n\n{proposed}\n"
    foot = text.rfind("\n---\n")
    if foot != -1:
        return text[:foot].rstrip() + block + text[foot:]
    return text.rstrip() + block


def apply_proposal(soul: str, prop: dict, *, force: bool = False) -> tuple[str, str]:
    """Apply one proposal to `soul`. Returns (new_soul, mode).

    Modes: ``replaced`` (the `current` text was found and rewritten in place),
    ``merged`` (a pure addition, folded into the existing section), ``created``
    (a pure addition for a section that doesn't exist yet).

    Raises StaleProposalError when `current` is set but missing, unless `force`
    — in which case the text is merged into the existing section. Either way a
    duplicate heading is never created.
    """
    section = (prop.get("section") or "").strip()
    proposed = (prop.get("proposed") or "").strip()
    current = (prop.get("current") or "").strip()

    if not proposed:
        raise StaleProposalError(section or "(unnamed)")

    proposed = _strip_trailing_footer(proposed, soul)

    if current:
        if current in soul:
            return soul.replace(current, proposed, 1), "replaced"
        if not force:
            raise StaleProposalError(section or "(unnamed)")

    span = find_section_span(soul, section)
    if span is None:
        return _append_section(soul, section, proposed), "created"

    start, end = span
    body = soul[start:end].rstrip()
    return f"{soul[:start]}{body}\n\n{proposed}\n\n{soul[end:].lstrip(chr(10))}", "merged"


def preview_proposals(soul: str, proposals: list) -> list[str]:
    """Dry-run a proposal document; returns the sections that would go stale.

    Used to flag proposals in the inbox *before* the user approves them.
    """
    stale: list[str] = []
    draft = soul
    for prop in proposals or []:
        try:
            draft, _ = apply_proposal(draft, prop)
        except StaleProposalError as e:
            stale.append(e.section)
    return stale


class SoulManager:
    """Manages ARIA's SOUL.md identity document."""

    def __init__(self):
        self._path = Path(os.path.expanduser(settings.soul_file))
        self._cache: Optional[str] = None
        self._cache_mtime: float = 0.0

    def ensure_file(self) -> None:
        """Create SOUL.md with default template if it doesn't exist."""
        if self._path.exists():
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(_DEFAULT_SOUL, encoding="utf-8")
        logger.info("Created default SOUL.md at %s", self._path)

    def read(self) -> str:
        """Read SOUL.md contents, with mtime-based caching."""
        if not self._path.exists():
            return ""
        try:
            mtime = self._path.stat().st_mtime
            if self._cache is not None and mtime == self._cache_mtime:
                return self._cache
            content = self._path.read_text(encoding="utf-8").strip()
            self._cache = content
            self._cache_mtime = mtime
            return content
        except Exception as e:
            logger.error("Failed to read SOUL.md: %s", e)
            return ""

    def write(self, content: str) -> str:
        """Write new content to SOUL.md. Returns the path written to."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(content, encoding="utf-8")
        self._cache = content.strip()
        self._cache_mtime = self._path.stat().st_mtime
        logger.info("Updated SOUL.md at %s", self._path)
        return str(self._path)

    @property
    def path(self) -> Path:
        return self._path


# Module-level singleton
soul_manager = SoulManager()
