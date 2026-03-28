"""
ARIA - Ambient Awareness (Passive Environmental Sensing)

Purpose: Pluggable sensor system that passively monitors git activity,
system state, and filesystem changes — building real-time situational
context without being asked.
"""

from aria.awareness.service import AwarenessService

__all__ = ["AwarenessService"]
