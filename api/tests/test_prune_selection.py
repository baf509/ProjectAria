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

    def sort(self, field, direction):
        assert field == "line_number" and direction == -1
        self._docs.sort(key=lambda d: d.get(field, 0), reverse=True)
        return self

    def batch_size(self, size):
        assert size == 2048
        return self


class _Events:
    def __init__(self, events):
        self.events = events            # shell_name -> newest/oldest event docs
        self.scanned = []
        self.page_limits = []
        self.distinct_calls = 0
        self.deleted = []

    async def distinct(self, field):
        self.distinct_calls += 1
        return list(self.events)

    def aggregate(self, pipeline, allowDiskUse=False):
        match = pipeline[0]["$match"]
        name = match["shell_name"]
        self.scanned.append(name)
        limit = pipeline[2]["$limit"]
        self.page_limits.append(limit)
        docs = sorted(self.events.get(name, []), key=lambda d: d["line_number"], reverse=True)
        before = match.get("line_number", {}).get("$lt")
        if before is not None:
            docs = [d for d in docs if d["line_number"] < before]
        docs = docs[:limit]
        if any("$group" in stage for stage in pipeline):
            if not docs:
                return _AggCursor([])
            return _AggCursor([{
                "_id": None,
                "chars": sum(len(d.get("text_clean") or "") for d in docs),
                "oldest": min(d["line_number"] for d in docs),
                "count": len(docs),
            }])
        remaining = next(
            stage["$match"]["cum"]["$gt"]
            for stage in pipeline if "$match" in stage and "cum" in stage["$match"]
        )
        total = 0
        for d in docs:
            total += len(d.get("text_clean") or "")
            if total > remaining:
                return _AggCursor([{"cutoff": d["line_number"]}])
        return _AggCursor([])

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
    def __init__(self, shells, events, state=None):
        self.shells = _Coll(shells)
        self.shell_events = _Events(events)
        self.shell_extraction_state = _Coll(state or [])


@pytest.mark.asyncio
async def test_shell_names_come_from_the_registry_not_a_distinct_scan():
    db = _DB(
        shells=[{"name": "a", "line_count": 10}, {"name": "b", "line_count": 20}],
        events={"a": [], "b": []},
    )
    await prune_shell_events(db, 150_000, dry_run=True)
    assert db.shell_events.distinct_calls == 0, "distinct() on shell_events must not be used"
    assert sorted(db.shell_events.scanned) == ["a", "b"]
    assert set(db.shell_events.page_limits) == {25_000}


@pytest.mark.asyncio
async def test_within_budget_shell_is_not_re_aggregated_until_it_grows():
    shells = [{"name": "a", "line_count": 10}]
    db = _DB(shells=shells, events={"a": []})
    memo: dict[str, int] = {}

    await prune_shell_events(db, 150_000, dry_run=True, within_budget=memo)
    assert db.shell_events.scanned == ["a"]
    assert memo == {"a": 10}

    # Second pass, unchanged line_count -> skipped entirely.
    await prune_shell_events(db, 150_000, dry_run=True, within_budget=memo)
    assert db.shell_events.scanned == ["a"], "shell was re-scanned despite no growth"

    # It grows past the memo -> aggregated again.
    shells[0]["line_count"] = 99
    await prune_shell_events(db, 150_000, dry_run=True, within_budget=memo)
    assert db.shell_events.scanned == ["a", "a"]


@pytest.mark.asyncio
async def test_no_memo_means_every_shell_is_evaluated():
    shells = [{"name": "a", "line_count": 10}]
    db = _DB(shells=shells, events={"a": []})
    await prune_shell_events(db, 150_000, dry_run=True)
    await prune_shell_events(db, 150_000, dry_run=True)
    assert db.shell_events.scanned == ["a", "a"]


@pytest.mark.asyncio
async def test_extraction_cursor_still_clamps_the_cutoff():
    db = _DB(
        shells=[{"name": "a", "line_count": 5000}],
        events={"a": [
            {"line_number": 5000, "text_clean": "x" * 400_000},
            {"line_number": 4000, "text_clean": "x" * 200_001},
        ]},
        state=[{"shell_name": "a", "last_line_extracted": 1200}],
    )
    await prune_shell_events(db, 150_000)
    assert db.shell_events.deleted == [{"shell_name": "a", "line_number": {"$lte": 1200}}], \
        "un-extracted events must still be protected by the cursor clamp"
    assert db.shell_events.scanned == ["a", "a"], "meta + bounded crossing page"
