"""
ARIA - Background task registry

Phase: Performance review (D11)
Purpose: Fire-and-forget coroutines that survive the garbage collector.

`asyncio.create_task` returns the only strong reference to a task; the event
loop keeps a weak one. Drop the return value and CPython may collect a task
that is still running -- "Task was destroyed but it is pending!" -- so the
work simply never finishes, silently and non-deterministically.

`Orchestrator._spawn_bg` already solves this for code with an instance to
hang a set off. This is the module-level version for code that has none.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Coroutine, Optional

logger = logging.getLogger(__name__)

# Strong references to in-flight tasks. Entries remove themselves on
# completion, so this is bounded by concurrency, not by total spawns.
_tasks: set[asyncio.Task] = set()


def spawn_bg(coro: Coroutine, name: Optional[str] = None) -> asyncio.Task:
    """Schedule a fire-and-forget coroutine, retaining a strong reference."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def pending_count() -> int:
    """How many spawned tasks are still in flight (tests and diagnostics)."""
    return len(_tasks)
