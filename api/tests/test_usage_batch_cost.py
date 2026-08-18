"""
ARIA - Batched conversation pricing (perf review D13)

Purpose: the project cockpit priced up to 25 sessions with 25 sequential
aggregations. `cost_for_conversations` answers for all of them in one, and
must produce exactly the same numbers as the per-conversation version.
"""

from datetime import datetime, timedelta, timezone

import pytest

from aria.db.usage import UsageRepo


class _FakeAggCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return self._docs[: length if length else None]


class _FakeUsage:
    """Groups in Python the way $group would, so the math is comparable."""

    def __init__(self, rows):
        self.rows = rows
        self.aggregate_calls = 0

    def aggregate(self, pipeline):
        self.aggregate_calls += 1
        match = pipeline[0]["$match"]
        wanted = match["conversation_id"]
        ids = wanted["$in"] if isinstance(wanted, dict) else [wanted]
        gid_keys = pipeline[1]["$group"]["_id"]
        grouped: dict[tuple, dict] = {}
        for r in self.rows:
            if r["conversation_id"] not in ids:
                continue
            key = tuple(
                r[v.lstrip("$")] for v in gid_keys.values()
            )
            g = grouped.setdefault(key, {
                "_id": {k: r[v.lstrip("$")] for k, v in gid_keys.items()},
                "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "requests": 0,
            })
            g["input_tokens"] += r["input_tokens"]
            g["output_tokens"] += r["output_tokens"]
            g["total_tokens"] += r["total_tokens"]
            g["requests"] += 1
        return _FakeAggCursor(list(grouped.values()))


class _FakeDB:
    def __init__(self, rows):
        self.usage = _FakeUsage(rows)


def _rows():
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    out = []
    for conv, model, n in [("c1", "claude-opus-4", 3), ("c1", "claude-sonnet-5", 2),
                           ("c2", "claude-opus-4", 1), ("c3", "local-ds4", 4)]:
        for _ in range(n):
            out.append({
                "conversation_id": conv, "model": model, "backend": "anthropic",
                "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                "timestamp": now,
            })
    return out


@pytest.mark.asyncio
async def test_batch_matches_per_conversation_and_runs_one_aggregation():
    convs = ["c1", "c2", "c3"]

    db = _FakeDB(_rows())
    repo = UsageRepo(db)
    per = {c: await repo.cost_for_conversation(c) for c in convs}
    assert db.usage.aggregate_calls == len(convs)

    db2 = _FakeDB(_rows())
    repo2 = UsageRepo(db2)
    batch = await repo2.cost_for_conversations(convs)
    assert db2.usage.aggregate_calls == 1, "batch must be a single aggregation"

    for c in convs:
        assert batch[c]["total_tokens"] == per[c]["total_tokens"]
        assert batch[c]["requests"] == per[c]["requests"]
        assert round(batch[c]["cost"], 6) == round(per[c]["cost"], 6)


@pytest.mark.asyncio
async def test_unknown_conversations_are_absent_not_zeroed():
    db = _FakeDB(_rows())
    batch = await UsageRepo(db).cost_for_conversations(["c1", "nope"])
    assert "c1" in batch and "nope" not in batch


@pytest.mark.asyncio
async def test_empty_and_duplicate_inputs():
    db = _FakeDB(_rows())
    repo = UsageRepo(db)
    assert await repo.cost_for_conversations([]) == {}
    assert db.usage.aggregate_calls == 0, "no ids must not hit the database"
    dup = await repo.cost_for_conversations(["c1", "c1", None, ""])
    assert dup["c1"]["requests"] == 5, "duplicate ids must not double-count"
