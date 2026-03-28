"""
ARIA - Research Models

Purpose: Internal data models for research runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class ResearchConfig:
    query: str
    depth: int = 2
    breadth: int = 3
    search_provider: str = "auto"
    llm_backend: str = "llamacpp"
    llm_model: str = "default"
    conversation_id: Optional[str] = None


@dataclass
class Learning:
    content: str
    source_url: Optional[str]
    confidence: float
    depth_found: int
    query_context: str


@dataclass
class ResearchProgress:
    current_depth: int = 0
    max_depth: int = 0
    queries_completed: int = 0
    queries_total: int = 0
    learnings_count: int = 0


@dataclass
class ResearchReport:
    query: str
    report_text: str
    learnings: list[Learning] = field(default_factory=list)
    total_tokens: int = 0
    duration_seconds: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
