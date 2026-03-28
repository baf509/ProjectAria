"""
ARIA - Base Sensor

Purpose: Abstract base class for all awareness sensors.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Observation:
    """A single observation from a sensor."""
    sensor: str
    category: str          # e.g. "git", "system", "filesystem"
    event_type: str        # e.g. "uncommitted_changes", "high_cpu", "new_file"
    summary: str           # Human-readable one-liner
    detail: Optional[str] = None  # Extended detail (diff, stats, etc.)
    severity: str = "info" # info, notice, warning
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_doc(self) -> dict:
        """Convert to MongoDB document."""
        return {
            "sensor": self.sensor,
            "category": self.category,
            "event_type": self.event_type,
            "summary": self.summary,
            "detail": self.detail,
            "severity": self.severity,
            "tags": self.tags,
            "created_at": self.created_at,
        }

    def to_context_line(self) -> str:
        """Format for injection into LLM context."""
        age = (datetime.now(timezone.utc) - self.created_at).total_seconds()
        if age < 60:
            ago = "just now"
        elif age < 3600:
            ago = f"{int(age // 60)}m ago"
        else:
            ago = f"{int(age // 3600)}h ago"
        return f"[{self.category}/{self.event_type}] {self.summary} ({ago})"


class BaseSensor(ABC):
    """Abstract base for awareness sensors."""

    name: str = "base"
    category: str = "unknown"

    @abstractmethod
    async def poll(self) -> list[Observation]:
        """Run one poll cycle. Returns new observations (may be empty)."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this sensor can run on the current system."""
        ...
