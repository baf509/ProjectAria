"""
ARIA - Usage Routes

Purpose: Usage aggregation endpoints.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.api.deps import get_db
from aria.db.usage import UsageRepo

router = APIRouter()


@router.get("/usage/summary")
async def usage_summary(
    days: int = 7,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get usage summary for the given time window."""
    repo = UsageRepo(db)
    return await repo.summary(days=days)


@router.get("/usage/by-agent")
async def usage_by_agent(
    days: int = 7,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get token totals grouped by agent."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pipeline = [
        {"$match": {"timestamp": {"$gte": cutoff}}},
        {
            "$group": {
                "_id": "$agent_slug",
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
                "total_tokens": {"$sum": "$total_tokens"},
                "requests": {"$sum": 1},
            }
        },
        {"$sort": {"total_tokens": -1}},
    ]
    return await db.usage.aggregate(pipeline).to_list(length=200)


@router.get("/usage/by-model")
async def usage_by_model(
    days: int = 7,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get token totals grouped by model."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pipeline = [
        {"$match": {"timestamp": {"$gte": cutoff}}},
        {
            "$group": {
                "_id": "$model",
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
                "total_tokens": {"$sum": "$total_tokens"},
                "requests": {"$sum": 1},
            }
        },
        {"$sort": {"total_tokens": -1}},
    ]
    return await db.usage.aggregate(pipeline).to_list(length=200)
