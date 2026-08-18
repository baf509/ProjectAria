"""
ARIA - Prune worker query hygiene (perf review D15)

Purpose: the prune worker used `db.shell_events.distinct("shell_name")` --
touching an 18.5M-row collection to learn a list of a few hundred names --
and then re-ran a per-shell windowed aggregation every 6 hours even for
shells that had not grown since the last pass.
"""

import pytest

from aria.shells.prune import prune_shell_events


class _AggCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()

    async def to_list(self, length=None):
        return self._docs


class _FindCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class _Events:
    def __init__(self, cutoffs):
        self.cutoffs = cutoffs          # shell_name -> cutoff or None
        self.aggregated = []
        self.distinct_calls = 0
        self.deleted = []

    async def distinct(self, field):
        self.distinct_calls += 1
        return list(self.cutoffs)

    def aggregate(self, pipeline, allowDiskUse=False):
        name = pipeline[0]["$match"]["shell_name"]
        self.aggregated.append(name)
        cut = self.cutoffs.get(name)
        return _AggCursor([] if cut is None else [{"_id": None, "cutoff": cut}])

    async def count_documents(self, flt):
        return 7

    async def delete_many(self, flt):
        self.deleted.append(flt)
        class R:
            deleted_count = 7
        return R()


class _Coll:
    def __init__(self, docs):
        self.docs = docs

    def find(self, flt=None, proj=None):
        return _FindCursor(self.docs)


class _DB:
    def __init__(self, shells, cutoffs, state=None):
        self.shells = _Coll(shells)
        self.shell_events = _Events(cutoffs)
        self.shell_extraction_state = _Coll(state or [])


@pytest.mark.asyncio
async def test_shell_names_come_from_the_registry_not_a_distinct_scan():
    db = _DB(
        shells=[{"name": "a", "line_count": 10}, {"name": "b", "line_count": 20}],
        cutoffs={"a": None, "b": None},
    )
    await prune_shell_events(db, 150_000, dry_run=True)
    assert db.shell_events.distinct_calls == 0, "distinct() on shell_events must not be used"
    assert sorted(db.shell_events.aggregated) == ["a", "b"]


@pytest.mark.asyncio
async def test_within_budget_shell_is_not_re_aggregated_until_it_grows():
    shells = [{"name": "a", "line_count": 10}]
    db = _DB(shells=shells, cutoffs={"a": None})
    memo: dict[str, int] = {}

    await prune_shell_events(db, 150_000, dry_run=True, within_budget=memo)
    assert db.shell_events.aggregated == ["a"]
    assert memo == {"a": 10}

    # Second pass, unchanged line_count -> skipped entirely.
    await prune_shell_events(db, 150_000, dry_run=True, within_budget=memo)
    assert db.shell_events.aggregated == ["a"], "shell was re-aggregated despite no growth"

    # It grows past the memo -> aggregated again.
    shells[0]["line_count"] = 99
    await prune_shell_events(db, 150_000, dry_run=True, within_budget=memo)
    assert db.shell_events.aggregated == ["a", "a"]


@pytest.mark.asyncio
async def test_no_memo_means_every_shell_is_evaluated():
    shells = [{"name": "a", "line_count": 10}]
    db = _DB(shells=shells, cutoffs={"a": None})
    await prune_shell_events(db, 150_000, dry_run=True)
    await prune_shell_events(db, 150_000, dry_run=True)
    assert db.shell_events.aggregated == ["a", "a"]


@pytest.mark.asyncio
async def test_extraction_cursor_still_clamps_the_cutoff():
    db = _DB(
        shells=[{"name": "a", "line_count": 5000}],
        cutoffs={"a": 4000},
        state=[{"shell_name": "a", "last_line_extracted": 1200}],
    )
    await prune_shell_events(db, 150_000)
    assert db.shell_events.deleted == [{"shell_name": "a", "line_number": {"$lte": 1200}}], \
        "un-extracted events must still be protected by the cursor clamp"
