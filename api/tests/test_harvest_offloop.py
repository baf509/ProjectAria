"""
ARIA - Project harvester must not block the event loop (perf review D9)

Purpose: harvest's discovery and aggregation are synchronous -- os.walk, two
`git` subprocesses per repo on 10 s timeouts, and jsonl session reads -- and
used to run directly on the event loop. Every 30 minutes the whole process
(HTTP, SSE, the coding watchdog's 5 s tick) stalled for the duration.

This test locks the fix: with a deliberately slow filesystem half, a
concurrent coroutine must keep getting scheduled.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from aria.shells import harvest as H


@pytest.mark.asyncio
async def test_discovery_does_not_stall_the_event_loop():
    BLOCK = 0.30  # how long the sync half "takes"

    def slow_find_git_repos(roots, max_depth=3):
        time.sleep(BLOCK)
        return []

    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    async def fake_gather_shells(db):
        return {}

    with patch.object(H, "_find_git_repos", slow_find_git_repos), \
         patch.object(H, "_gather_claude", lambda: {}), \
         patch.object(H, "_gather_pi", lambda: {}), \
         patch.object(H, "_gather_shells", fake_gather_shells):
        t = asyncio.create_task(ticker())
        await asyncio.sleep(0)  # let the ticker start
        await H.harvest(db=None)
        stop.set()
        await t

    # On-loop, the ticker would be frozen for the whole BLOCK and land at ~0.
    # Off-loop it should get roughly BLOCK/0.01 ticks; assert well clear of 0
    # without being flaky about scheduler jitter.
    assert ticks >= 10, f"event loop was starved during harvest discovery ({ticks} ticks)"


@pytest.mark.asyncio
async def test_aggregation_git_probes_are_off_loop():
    """The per-repo git calls are the bulk of the wall-clock, not just the walk."""
    BLOCK = 0.25

    git_calls = []

    def slow_git(path, *args):
        git_calls.append(path)
        time.sleep(BLOCK)
        return None

    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    async def fake_gather_shells(db):
        return {}

    # One discovered repo -> the aggregation loop runs and hits _git.
    with patch.object(H, "_find_git_repos", lambda roots, max_depth=3: []), \
         patch.object(H, "_gather_claude", lambda: {"/tmp/fake-repo": {"sessions": 1, "last_activity": None}}), \
         patch.object(H, "_gather_pi", lambda: {}), \
         patch.object(H, "_gather_shells", fake_gather_shells), \
         patch.object(H, "_git", slow_git), \
         patch("aria.shells.harvest.Path") as FakePath:
        FakePath.return_value.__truediv__.return_value.exists.return_value = True
        t = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        try:
            await H.harvest(db=None)
        except Exception:
            pass  # the fake db is None; we only care that the loop kept running
        stop.set()
        await t

    assert git_calls, "test is vacuous: the aggregation loop never reached _git"
    assert ticks >= 8, f"event loop was starved during aggregation ({ticks} ticks)"
