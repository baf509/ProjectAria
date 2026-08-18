"""
ARIA - Fire-and-forget task retention (perf review D11)

Purpose: a background task must not be garbage-collected while still running.

`asyncio.create_task` hands back the only strong reference; the loop keeps a
weak one. Discard the return value and CPython may collect a pending task --
"Task was destroyed but it is pending!" -- so the work silently never happens.
This is why `spawn_bg` exists, and why the grep gate below refuses new bare
call sites.
"""

import asyncio
import re
from pathlib import Path

import pytest

from aria.core import bg

ARIA_ROOT = Path(__file__).resolve().parents[1] / "aria"

# Held in `running.readers`, so these two already keep strong references.
ALLOWED_BARE = {"agents/subprocess_mgr.py"}


@pytest.mark.asyncio
async def test_spawn_bg_retains_reference_until_done():
    started = asyncio.Event()
    finished = asyncio.Event()

    async def work():
        started.set()
        await asyncio.sleep(0.02)
        finished.set()

    bg.spawn_bg(work(), name="test:work")   # deliberately not assigned
    await started.wait()
    assert bg.pending_count() >= 1, "task dropped out of the registry while running"
    await asyncio.wait_for(finished.wait(), timeout=1)
    await asyncio.sleep(0)  # let the done-callback run
    assert bg.pending_count() == 0, "registry leaked a completed task"


@pytest.mark.asyncio
async def test_registry_does_not_grow_across_many_spawns():
    async def noop():
        return None

    for _ in range(50):
        bg.spawn_bg(noop())
    await asyncio.sleep(0.05)
    assert bg.pending_count() == 0


def test_no_new_bare_create_task_call_sites():
    """A discarded create_task is the bug D11 fixed -- keep it fixed."""
    bare = re.compile(r"^\s*asyncio\.create_task\(")
    offenders = []
    for path in ARIA_ROOT.rglob("*.py"):
        rel = path.relative_to(ARIA_ROOT).as_posix()
        if rel in ALLOWED_BARE:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if bare.match(line):
                offenders.append(f"{rel}:{n}")
    assert not offenders, (
        "asyncio.create_task result discarded (task can be GC'd mid-flight); "
        "use aria.core.bg.spawn_bg or keep a reference: " + ", ".join(offenders)
    )
