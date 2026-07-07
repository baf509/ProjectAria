"""
ARIA - Watched Shells Subsystem

Purpose: Observe and relay user-owned tmux coding sessions into MongoDB,
integrate them with ARIA's memory and chat context, and allow remote
control via the API and dashboard.

This module is deliberately parallel to agents/ — ARIA does not own the
lifecycle of a watched shell; tmux does. Current behavior is documented in
CLAUDE.md ("Watched Shells & Fleet"); the original design rationale is in
docs/archive/SHELLS_DESIGN.md (historical).
"""

from aria.shells.models import Shell, ShellEvent, ShellSnapshot, ShellInput
from aria.shells.service import ShellService, ShellNotFoundError, ShellStoppedError

__all__ = [
    "Shell",
    "ShellEvent",
    "ShellSnapshot",
    "ShellInput",
    "ShellService",
    "ShellNotFoundError",
    "ShellStoppedError",
]
