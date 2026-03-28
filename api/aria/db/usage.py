"""
ARIA - Usage Repository

Purpose: Persist model usage metadata for later aggregation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase


class UsageRepo:
    """Persistence helpers for model usage tracking."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db

    async def record(
        self,
        *,
        model: str,
        source: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        agent_slug: Optional[str] = None,
        conversation_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        doc = {
            "model": model,
            "source": source,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "agent_slug": agent_slug,
            "conversation_id": conversation_id,
            "metadata": metadata or {},
            "timestamp": datetime.now(timezone.utc),
        }
        result = await self.db.usage.insert_one(doc)
        return str(result.inserted_id)

    async def summary(self, days: int = 7) -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        pipeline = [
            {"$match": {"timestamp": {"$gte": cutoff}}},
            {
                "$group": {
                    "_id": None,
                    "input_tokens": {"$sum": "$input_tokens"},
                    "output_tokens": {"$sum": "$output_tokens"},
                    "total_tokens": {"$sum": "$total_tokens"},
                    "requests": {"$sum": 1},
                }
            },
        ]
        result = await self.db.usage.aggregate(pipeline).to_list(length=1)
        return result[0] if result else {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "requests": 0,
        }
