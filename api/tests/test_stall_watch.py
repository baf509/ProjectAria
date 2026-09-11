"""A wedged backend must be noticed; a slow one must not be accused."""

import pytest

from aria.infrastructure.stall_watch import BackendStallWatcher


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _watcher(rows, clock, stall_after=1200.0):
    """rows: a list consumed one per tick. alerts land in watcher.raised."""
    sent = []

    async def probe():
        return rows.pop(0) if rows else {}

    async def alert(**kwargs):
        sent.append(kwargs)

    w = BackendStallWatcher(probe=probe, alert=alert, stall_after_seconds=stall_after,
                            clock=clock)
    w.raised = sent
    return w


@pytest.mark.asyncio
async def test_frozen_token_counter_while_processing_raises():
    """The 2026-09-11 wedge: slot busy, token counter frozen, nothing computing."""
    clock = _Clock()
    busy = {"cand": {"requests_processing": 1, "tokens_predicted_total": 4244}}
    w = _watcher([dict(busy), dict(busy), dict(busy)], clock)

    assert await w.tick() == []          # first sighting is the reference point
    clock.advance(600)
    assert await w.tick() == []          # 600s frozen: under the floor, stay quiet
    clock.advance(900)
    stalled = await w.tick()             # 1500s frozen: past the floor

    assert [s["slug"] for s in stalled] == ["cand"]
    assert stalled[0]["stalled_seconds"] == 1500
    assert len(w.raised) == 1
    assert w.raised[0]["event_type"] == "backend_stalled"
    assert w.raised[0]["severity"] == "critical"
    assert w.raised[0]["dedup_key"] == "backend_stalled:cand"


@pytest.mark.asyncio
async def test_slow_generation_is_not_a_stall():
    """A backend crawling at a few tokens per poll is working, not wedged."""
    clock = _Clock()
    rows = [{"cand": {"requests_processing": 1, "tokens_predicted_total": 100 + n}}
            for n in range(4)]
    w = _watcher(rows, clock)

    for _ in range(4):
        clock.advance(3600)              # far past the floor between polls
        assert await w.tick() == []
    assert w.raised == []


@pytest.mark.asyncio
async def test_idle_backend_never_alerts_and_clears_history():
    """Zero in-flight work is idle, not stalled — even if tokens never move."""
    clock = _Clock()
    frozen = {"cand": {"requests_processing": 1, "tokens_predicted_total": 7}}
    idle = {"cand": {"requests_processing": 0, "tokens_predicted_total": 7}}
    w = _watcher([dict(frozen), dict(idle), dict(frozen)], clock)

    await w.tick()
    clock.advance(5000)
    assert await w.tick() == []          # went idle: history forgotten
    clock.advance(5000)
    assert await w.tick() == []          # busy again, but this is a fresh start
    assert w.raised == []


@pytest.mark.asyncio
async def test_alert_is_raised_once_per_stall_not_every_tick():
    clock = _Clock()
    busy = {"cand": {"requests_processing": 1, "tokens_predicted_total": 4244}}
    w = _watcher([dict(busy) for _ in range(4)], clock)

    await w.tick()
    clock.advance(2000)
    assert len(await w.tick()) == 1
    clock.advance(2000)
    assert await w.tick() == []          # still wedged, already reported
    assert len(w.raised) == 1


@pytest.mark.asyncio
async def test_recovery_rearms_the_alert():
    """After it recovers, a later wedge must be reported again."""
    clock = _Clock()
    w = _watcher([
        {"cand": {"requests_processing": 1, "tokens_predicted_total": 10}},
        {"cand": {"requests_processing": 1, "tokens_predicted_total": 10}},
        {"cand": {"requests_processing": 0, "tokens_predicted_total": 99}},
        {"cand": {"requests_processing": 1, "tokens_predicted_total": 99}},
        {"cand": {"requests_processing": 1, "tokens_predicted_total": 99}},
    ], clock)

    await w.tick()
    clock.advance(2000)
    assert len(await w.tick()) == 1
    await w.tick()                       # recovered
    await w.tick()                       # busy again
    clock.advance(2000)
    assert len(await w.tick()) == 1
    assert len(w.raised) == 2


@pytest.mark.asyncio
async def test_missing_counter_is_not_evidence_of_a_stall():
    """A backend that does not report tokens can never be judged frozen."""
    clock = _Clock()
    rows = [{"cand": {"requests_processing": 1, "tokens_predicted_total": None}}
            for _ in range(3)]
    w = _watcher(rows, clock)

    for _ in range(3):
        clock.advance(4000)
        assert await w.tick() == []
    assert w.raised == []


@pytest.mark.asyncio
async def test_a_failing_probe_does_not_kill_the_watcher():
    clock = _Clock()
    calls = []

    async def probe():
        calls.append(1)
        raise RuntimeError("backend unreachable")

    async def alert(**kwargs):
        pass

    w = BackendStallWatcher(probe=probe, alert=alert, clock=clock)
    with pytest.raises(RuntimeError):
        await w.tick()
    assert calls == [1]


@pytest.mark.asyncio
async def test_alert_failure_does_not_stop_detection():
    """The log line is the durable half; a down alert backend must not raise."""
    clock = _Clock()
    busy = {"cand": {"requests_processing": 1, "tokens_predicted_total": 4244}}

    async def probe():
        return dict(busy)

    async def alert(**kwargs):
        raise RuntimeError("notifier down")

    w = BackendStallWatcher(probe=probe, alert=alert, stall_after_seconds=100,
                            clock=clock)
    await w.tick()
    clock.advance(500)
    assert len(await w.tick()) == 1      # still reported to the caller
