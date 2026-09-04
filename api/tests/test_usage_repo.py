"""
Tests for aria.db.usage.UsageRepo.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from aria.api.routes.usage import usage_by_caller
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

    def test_hit_rate_math(self):
        # 900 cached of 1000 total prompt tokens -> 0.9
        assert UsageRepo._hit_rate(900, 100) == 0.9
        assert UsageRepo._hit_rate(0, 0) == 0.0
        assert UsageRepo._hit_rate(0, 100) == 0.0

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
