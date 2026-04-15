"""
ARIA - Watched Shells Subsystem

Purpose: Observe and relay user-owned tmux coding sessions into MongoDB,
integrate them with ARIA's memory and chat context, and allow remote
control via the API and dashboard.

This module is deliberately parallel to agents/ — ARIA does not own the
lifecycle of a watched shell; tmux does. See SHELLS_DESIGN.md.
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
