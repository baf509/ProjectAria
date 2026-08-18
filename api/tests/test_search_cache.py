"""
ARIA - Memory search cache bounds (perf review D10)

Purpose: the search cache must stay bounded under a burst of distinct queries.

The bug this locks: `_SearchCache.put` only swept expired entries when the
cache was ALREADY over 100 entries, and stopped after the sweep. A burst of
more than 100 distinct queries inside one TTL window therefore grew the cache
without bound for the length of the burst -- and every entry holds a full copy
of a result list, so the growth is real memory, not just keys.
"""

import time

from aria.memory.long_term import _SearchCache


def _put(cache, i, n_results=3):
    cache.put(f"query-{i}", 10, None, [{"id": f"m{i}-{j}"} for j in range(n_results)])


def test_burst_of_distinct_queries_stays_capped():
    cache = _SearchCache(ttl_seconds=3600)  # nothing expires during the burst
    for i in range(300):
        _put(cache, i)
    assert len(cache._cache) <= _SearchCache.MAX_ENTRIES, (
        f"cache grew to {len(cache._cache)} entries, cap is {_SearchCache.MAX_ENTRIES}"
    )


def test_eviction_drops_oldest_first_and_keeps_newest():
    cache = _SearchCache(ttl_seconds=3600)
    for i in range(_SearchCache.MAX_ENTRIES + 50):
        _put(cache, i)
    # The most recent query must still be servable; the first must be gone.
    assert cache.get("query-%d" % (_SearchCache.MAX_ENTRIES + 49), 10, None) is not None
    assert cache.get("query-0", 10, None) is None


def test_expired_entries_are_swept_before_oldest_eviction():
    cache = _SearchCache(ttl_seconds=1)
    for i in range(200):
        _put(cache, i)
    time.sleep(1.05)  # everything above is now stale
    for i in range(200, 200 + _SearchCache.MAX_ENTRIES):
        _put(cache, i)
    assert len(cache._cache) <= _SearchCache.MAX_ENTRIES
    # The sweep should have reclaimed the stale entries, so the fresh ones all
    # survive rather than being evicted to make room for each other.
    assert cache.get("query-%d" % (200 + _SearchCache.MAX_ENTRIES - 1), 10, None) is not None


def test_cached_results_are_copies_not_aliases():
    cache = _SearchCache(ttl_seconds=3600)
    _put(cache, 1)
    got = cache.get("query-1", 10, None)
    got.append({"id": "injected"})
    assert len(cache.get("query-1", 10, None)) == 3
