"""
Tests for aria.db.usage.UsageRepo.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from aria.api.routes.usage import _inference_trace_row, usage_by_caller, usage_traces
from aria.db.usage import UsageRepo
from tests.conftest import make_mock_db


class TestUsageRepo:

    @pytest.mark.asyncio
    async def test_record_creates_doc(self):
        db = make_mock_db()
        repo = UsageRepo(db)

        result_id = await repo.record(
            model="gpt-4",
            source="chat",
            input_tokens=100,
            output_tokens=50,
            agent_slug="aria",
            conversation_id="conv-1",
            caller="hermes",
            trace_id="trace-1",
            preamble_hash="prefix-1",
        )

        db.usage.insert_one.assert_called_once()
        doc = db.usage.insert_one.call_args[0][0]
        assert doc["model"] == "gpt-4"
        assert doc["source"] == "chat"
        assert doc["input_tokens"] == 100
        assert doc["output_tokens"] == 50
        assert doc["agent_slug"] == "aria"
        assert doc["conversation_id"] == "conv-1"
        assert doc["caller"] == "hermes"
        assert doc["trace_id"] == "trace-1"
        assert doc["preamble_hash"] == "prefix-1"
        assert doc["timestamp"] is not None
        assert result_id == "mock-id"

    @pytest.mark.asyncio
    async def test_record_stores_cache_tokens(self):
        db = make_mock_db()
        repo = UsageRepo(db)
        await repo.record(
            model="claude-sonnet-5", source="chat",
            input_tokens=100, output_tokens=50,
            cache_read_tokens=900, cache_write_tokens=40,
        )
        doc = db.usage.insert_one.call_args[0][0]
        assert doc["cache_read_tokens"] == 900
        assert doc["cache_write_tokens"] == 40
        assert "trace_id" not in doc
        assert "preamble_hash" not in doc

    def test_hit_rate_math(self):
        # 900 cached of 1000 total prompt tokens -> 0.9
        assert UsageRepo._hit_rate(900, 100) == 0.9
        assert UsageRepo._hit_rate(0, 0) == 0.0
        assert UsageRepo._hit_rate(0, 100) == 0.0

    def test_inference_trace_projection_is_content_free_and_complete(self):
        row = _inference_trace_row({
            "trace_id": "abc123",
            "caller": "pi-coding-mac",
            "model": "hybrid",
            "input_tokens": 20,
            "cache_read_tokens": 80,
            "output_tokens": 10,
            "metadata": {
                "path": "chat/completions",
                "outcome": "ok",
                "latency_ms": 500,
                "queue_wait_ms": 12,
                "context_tokens": 110,
                "decode_tokens_per_second": 50.0,
                "speculative_acceptance_rate": 0.7,
                "preamble": {
                    "state": "changed",
                    "change_reason": "volatile_timestamp",
                    "fingerprint": "new",
                    "previous_fingerprint": "old",
                    "prefix_bytes": 4096,
                    "tool_count": 12,
                },
                "private_prompt": "must not escape",
            },
        })

        assert row["trace_id"] == "abc123"
        assert row["cache_hit_rate"] == 0.8
        assert row["context_tokens"] == 110
        assert row["preamble"]["change_reason"] == "volatile_timestamp"
        assert "must not escape" not in repr(row)

    @pytest.mark.asyncio
    async def test_by_caller_reports_weighted_cache_effectiveness(self):
        db = make_mock_db()
        db.usage.aggregate.return_value.to_list = AsyncMock(return_value=[{
            "_id": "pi-coding/mac",
            "backend": "llamacpp",
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cache_read_tokens": 900,
            "requests": 4,
        }])

        rows = await usage_by_caller(days=7, db=db)

        assert rows[0]["cache_hit_rate"] == 0.9
        pipeline = db.usage.aggregate.call_args.args[0]
        assert pipeline[0]["$match"]["caller"] == {"$nin": [None, ""]}

    @pytest.mark.asyncio
    async def test_usage_traces_filters_caller_and_returns_projection(self):
        db = make_mock_db()
        db.usage.find.return_value.to_list = AsyncMock(return_value=[{
            "trace_id": "trace-1",
            "caller": "hermes",
            "model": "hybrid",
            "input_tokens": 5,
            "cache_read_tokens": 45,
            "output_tokens": 2,
            "metadata": {"outcome": "ok", "preamble": {"state": "stable"}},
        }])

        rows = await usage_traces(hours=24, limit=1, caller="hermes", db=db)

        query = db.usage.find.call_args.args[0]
        assert query["source"] == "llm-gateway"
        assert query["caller"] == "hermes"
        assert rows[0]["trace_id"] == "trace-1"
        assert rows[0]["cache_hit_rate"] == 0.9

    @pytest.mark.asyncio
    async def test_record_total_tokens_computed(self):
        db = make_mock_db()
        repo = UsageRepo(db)

        await repo.record(model="gpt-4", source="chat", input_tokens=100, output_tokens=50)

        doc = db.usage.insert_one.call_args[0][0]
        assert doc["total_tokens"] == 150

    @pytest.mark.asyncio
    async def test_summary_returns_aggregation(self):
        db = make_mock_db()
        agg_result = {
            "_id": None,
            "input_tokens": 500,
            "output_tokens": 250,
            "total_tokens": 750,
            "requests": 10,
        }
        db.usage.aggregate.return_value.to_list = AsyncMock(return_value=[agg_result])
        repo = UsageRepo(db)

        result = await repo.summary(days=7)

        assert result["input_tokens"] == 500
        assert result["output_tokens"] == 250
        assert result["total_tokens"] == 750
        assert result["requests"] == 10

    @pytest.mark.asyncio
    async def test_summary_empty(self):
        db = make_mock_db()
        db.usage.aggregate.return_value.to_list = AsyncMock(return_value=[])
        repo = UsageRepo(db)

        result = await repo.summary(days=7)

        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0
        assert result["total_tokens"] == 0
        assert result["requests"] == 0
