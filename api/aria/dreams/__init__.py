"""
ARIA - Dream Cycle

Purpose: Offline reflection engine that runs during quiet hours.
Uses Claude Code CLI (subscription tokens) for heavy LLM work
rather than consuming API tokens.
"""

from aria.dreams.service import DreamService
from aria.dreams.collector import DreamCollector

__all__ = ["DreamService", "DreamCollector"]
