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
from aria.llm.pricing import cost_for

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
                "backend": {"$first": "$backend"},
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
                "total_tokens": {"$sum": "$total_tokens"},
                "requests": {"$sum": 1},
            }
        },
        {"$sort": {"total_tokens": -1}},
    ]
    rows = await db.usage.aggregate(pipeline).to_list(length=200)
    for r in rows:
        r["cost"] = round(
            cost_for(r["_id"], r.get("input_tokens", 0), r.get("output_tokens", 0), r.get("backend")),
            6,
        )
    return rows


@router.get("/usage/cost")
async def usage_cost(
    days: int = 7,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Total $ cost over the window with a per-(model, backend) breakdown."""
    return await UsageRepo(db).cost_summary(days=days)


@router.get("/usage/by-conversation")
async def usage_by_conversation(
    days: int = 7,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Token + cost totals grouped by conversation."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pipeline = [
        {"$match": {"timestamp": {"$gte": cutoff}, "conversation_id": {"$ne": None}}},
        {"$group": {
            "_id": {"conversation_id": "$conversation_id", "model": "$model", "backend": "$backend"},
            "input_tokens": {"$sum": "$input_tokens"},
            "output_tokens": {"$sum": "$output_tokens"},
            "total_tokens": {"$sum": "$total_tokens"},
            "requests": {"$sum": 1},
        }},
    ]
    rows = await db.usage.aggregate(pipeline).to_list(length=1000)
    by_conv: dict = {}
    for r in rows:
        gid = r["_id"]
        conv = gid["conversation_id"]
        c = by_conv.setdefault(conv, {
            "conversation_id": conv, "input_tokens": 0, "output_tokens": 0,
            "total_tokens": 0, "requests": 0, "cost": 0.0,
        })
        c["input_tokens"] += r.get("input_tokens", 0)
        c["output_tokens"] += r.get("output_tokens", 0)
        c["total_tokens"] += r.get("total_tokens", 0)
        c["requests"] += r.get("requests", 0)
        c["cost"] += cost_for(gid.get("model"), r.get("input_tokens", 0), r.get("output_tokens", 0), gid.get("backend"))
    out = sorted(by_conv.values(), key=lambda x: -x["cost"])
    for c in out:
        c["cost"] = round(c["cost"], 6)
    return out


@router.get("/usage/by-session")
async def usage_by_session(
    days: int = 30,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Per coding-session token + cost totals, mapped via each session's
    working conversation. Powers the fleet view's cost column."""
    repo = UsageRepo(db)
    sessions = await db.coding_sessions.find({}).sort("created_at", -1).to_list(length=200)
    out = []
    for s in sessions:
        conv = s.get("agent_conversation_id") or s.get("conversation_id")
        cost = await repo.cost_for_conversation(conv, days=days) if conv else {}
        out.append({
            "session_id": s["_id"],
            "backend": s.get("backend"),
            "llm": s.get("llm"),
            "model": s.get("model"),
            "status": s.get("status"),
            "total_tokens": cost.get("total_tokens", 0),
            "cost": cost.get("cost", 0.0),
        })
    return out
