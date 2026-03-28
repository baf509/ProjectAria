"""
ARIA - Workflow Models

Purpose: Internal workflow model helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkflowStep:
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[int] = field(default_factory=list)


@dataclass
class Workflow:
    name: str
    description: str
    steps: list[WorkflowStep]
    tags: list[str] = field(default_factory=list)
