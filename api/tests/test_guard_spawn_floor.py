"""The guard's spawn floor, asserted on purpose.

This behaviour was previously exercised only by ACCIDENT: `guard_preflight`
reads live /proc/meminfo, so whether a coding-session test hit the floor
depended on how loaded the box was. On 2026-08-17 eleven tests flipped to
failing — same code, 6 GiB free instead of 11 — with a message pointing at the
guard rather than at the test.

conftest now pins the reading so those tests are deterministic. The floor itself
still needs covering, so it is covered HERE, where the value is chosen by the
test rather than by whatever else is running.
"""
from unittest.mock import patch

from aria.config import settings
from aria.guard import sandbox as gs


def _preflight_with_mem(value):
    with patch.object(gs, "mem_available_gib", lambda: value):
        return gs.preflight()


def test_refuses_below_the_floor():
    """Under the floor => refuse. A spawn under it OOM-kills a resident model,
    which on this box means evicting ~100 GiB of weights that take minutes to
    reload."""
    res = _preflight_with_mem(settings.guard_min_mem_available_gib - 1.0)
    assert res["spawn_allowed"] is False
    assert any("spawn floor" in r for r in res["reasons"])


def test_allows_above_the_floor():
    res = _preflight_with_mem(settings.guard_min_mem_available_gib + 1.0)
    assert any("spawn floor" in r for r in res["reasons"]) is False


def test_unreadable_memory_refuses_rather_than_guessing():
    """FAIL CLOSED. An unreadable MemAvailable is not "probably fine" — the
    floor exists precisely because the consequence is an OOM-killed model."""
    res = _preflight_with_mem(None)
    assert res["spawn_allowed"] is False
    assert any("could not be read" in r for r in res["reasons"])


def test_the_boundary_is_inclusive_of_the_floor_value():
    """Exactly AT the floor is allowed; the check is `mem < floor`. Pinned
    because an off-by-one here changes whether a marginal box can spawn at all."""
    res = _preflight_with_mem(float(settings.guard_min_mem_available_gib))
    assert any("spawn floor" in r for r in res["reasons"]) is False
