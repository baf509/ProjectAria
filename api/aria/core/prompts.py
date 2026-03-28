"""
ARIA - Prompt Template Loader

Purpose: Load prompt templates from .md files with placeholder substitution.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


@lru_cache(maxsize=32)
def _read_template(name: str) -> str:
    """Read and cache a raw template file."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def load_prompt(name: str, **kwargs: str) -> str:
    """Load a prompt template and substitute placeholders.

    Templates use Python str.format() syntax:
      - {placeholder} for substitution
      - {{ and }} for literal braces (e.g. JSON examples)
    """
    template = _read_template(name)
    if kwargs:
        return template.format(**kwargs)
    return template


def reload_templates() -> None:
    """Clear the template cache (e.g. after editing prompt files)."""
    _read_template.cache_clear()
