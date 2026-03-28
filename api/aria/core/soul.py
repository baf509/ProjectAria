"""
ARIA - Soul Manager

Purpose: Manage ARIA's persistent identity document (SOUL.md)

The SOUL.md file defines ARIA's personality, values, boundaries, and
communication style. It is loaded into the system prompt on every
conversation and can be updated by the agent itself.
"""

import logging
import os
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
