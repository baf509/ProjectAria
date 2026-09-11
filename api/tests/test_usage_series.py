"""
Tests for aria.db.usage.UsageRepo.series() -- the bucketed token history that
backs the usage charts.

The behaviour worth protecting here is not the arithmetic, it is the two
honesty rules: a bucket nobody reported reuse for must not read as 0% reuse,
and a series must keep its identity when it shrinks.
"""

from datetime import datetime, timezone

import pytest

from aria.db.usage import UsageRepo
from tests.conftest import make_mock_db


def _row(when: datetime, key: str | None, *, total: int, cache_read: int = 0,
         reported_input: int = 0, reported_requests: int = 1, requests: int = 1) -> dict:
    return {
        "_id": {"t": when, "k": key},
        "input_tokens": reported_input,
        "output_tokens": 0,
        "total_tokens": total,
        "requests": requests,
        "cache_read_tokens": cache_read,
        "reported_input_tokens": reported_input,
        "reported_requests": reported_requests,
    }


def _repo_returning(rows: list[dict]) -> UsageRepo:
    db = make_mock_db()
    db.usage.aggregate.return_value.to_list.return_value = rows
    return UsageRepo(db)


D1 = datetime(2026, 9, 10, tzinfo=timezone.utc)
D2 = datetime(2026, 9, 11, tzinfo=timezone.utc)


class TestSeriesShape:

    @pytest.mark.asyncio
    async def test_buckets_are_summed_and_sorted(self):
        repo = _repo_returning([
            _row(D2, "b", total=5),
            _row(D1, "a", total=10),
            _row(D1, "b", total=3),
        ])
        out = await repo.series(days=30, bucket="day", by="model")

        assert [b["t"] for b in out["buckets"]] == [D1, D2]
        assert out["buckets"][0]["total_tokens"] == 13
        assert out["buckets"][0]["by"] == {"a": 10, "b": 3}
        assert out["buckets"][1]["by"] == {"b": 5}

    @pytest.mark.asyncio
    async def test_empty_buckets_are_absent_not_zero_filled(self):
        """A day with no requests is not a day with zero tokens -- the caller
        decides whether a hole is a zero (tokens) or a gap (a rate)."""
        repo = _repo_returning([_row(D1, "a", total=10)])
        out = await repo.series(days=30, bucket="day", by="model")
        assert len(out["buckets"]) == 1

    @pytest.mark.asyncio
    async def test_rejects_unknown_bucket_and_dimension(self):
        repo = _repo_returning([])
        with pytest.raises(ValueError):
            await repo.series(bucket="fortnight")
        with pytest.raises(ValueError):
            await repo.series(by="colour")

    @pytest.mark.asyncio
    async def test_dateTrunc_unit_follows_the_bucket(self):
        repo = _repo_returning([])
        await repo.series(bucket="hour", by="none")
        pipeline = repo.db.usage.aggregate.call_args[0][0]
        assert pipeline[1]["$group"]["_id"]["t"]["$dateTrunc"]["unit"] == "hour"
        # `by=none` must not add a grouping key at all.
        assert "k" not in pipeline[1]["$group"]["_id"]


class TestSeriesCacheHonesty:

    @pytest.mark.asyncio
    async def test_bucket_with_no_reporting_backend_has_no_rate(self):
        """The whole point of `cache_reporting`: a backend that never answers
        must not be charted as a backend that reused nothing."""
        repo = _repo_returning([
            _row(D1, "red", total=100, cache_read=0, reported_input=0, reported_requests=0),
        ])
        out = await repo.series(days=7, bucket="day", by="model")
        bucket = out["buckets"][0]
        assert bucket["cache_hit_rate"] is None
        assert bucket["cache_reporting"] == "unsupported"

    @pytest.mark.asyncio
    async def test_rate_is_computed_over_reporting_requests_only(self):
        repo = _repo_returning([
            _row(D1, "a", total=200, cache_read=900, reported_input=100, reported_requests=1),
            _row(D1, "b", total=100, cache_read=0, reported_input=0, reported_requests=0),
        ])
        bucket = (await repo.series(days=7, bucket="day", by="model"))["buckets"][0]
        assert bucket["cache_hit_rate"] == 0.9      # 900 / (900 + 100)
        assert bucket["cache_reporting"] == "partial"

    @pytest.mark.asyncio
    async def test_reporting_breakdown_is_not_leaked(self):
        repo = _repo_returning([_row(D1, "a", total=10, cache_read=5, reported_input=5)])
        bucket = (await repo.series(days=7, bucket="day", by="model"))["buckets"][0]
        assert "reported_input_tokens" not in bucket
        assert "reported_requests" not in bucket


class TestSeriesRanking:

    @pytest.mark.asyncio
    async def test_folds_past_top_into_other(self):
        rows = [_row(D1, name, total=total) for name, total in
                [("a", 50), ("b", 40), ("c", 30), ("d", 20), ("e", 10), ("f", 5)]]
        out = await _repo_returning(rows).series(days=7, bucket="day", by="model", top=4)

        assert out["series"] == ["a", "b", "c", "d", "Other"]
        assert out["buckets"][0]["by"]["Other"] == 15   # e + f
        assert "e" not in out["buckets"][0]["by"]

    @pytest.mark.asyncio
    async def test_rank_is_taken_over_the_whole_window(self):
        """A series that is small in the last bucket but large overall keeps its
        own slot -- otherwise its colour would change as the window scrolls."""
        rows = [
            _row(D1, "big", total=1000), _row(D1, "small", total=1),
            _row(D2, "big", total=1), _row(D2, "small", total=5),
        ]
        out = await _repo_returning(rows).series(days=7, bucket="day", by="model", top=1)
        assert out["series"] == ["big", "Other"]
        assert out["buckets"][1]["by"] == {"big": 1, "Other": 5}

    @pytest.mark.asyncio
    async def test_missing_dimension_value_is_named_not_blanked(self):
        """An absent agent is `unattributed`, which is a different fact from an
        agent literally called "unknown"."""
        out = await _repo_returning([
            _row(D1, None, total=10),
            _row(D1, "unknown", total=4),
        ]).series(days=7, bucket="day", by="agent")
        assert out["buckets"][0]["by"] == {"unattributed": 10, "unknown": 4}

    @pytest.mark.asyncio
    async def test_by_none_reports_totals_with_no_series(self):
        out = await _repo_returning([
            _row(D1, None, total=10), _row(D2, None, total=7),
        ]).series(days=7, bucket="day", by="none")
        assert out["series"] == []
        assert [b["total_tokens"] for b in out["buckets"]] == [10, 7]
        assert all(b["by"] == {} for b in out["buckets"])
