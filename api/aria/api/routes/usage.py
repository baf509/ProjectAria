"""
ARIA - Usage Routes

Purpose: Usage aggregation endpoints.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.api.deps import get_db
from aria.db.usage import UsageRepo
from aria.llm.pricing import cost_for

router = APIRouter()


def _inference_trace_row(doc: dict) -> dict:
    """Public, content-free projection of one gateway usage document."""
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    preamble = (
        metadata.get("preamble")
        if isinstance(metadata.get("preamble"), dict)
        else {"state": "absent", "change_reason": None}
    )
    cached = doc.get("cache_read_tokens", 0) or 0
    fresh = doc.get("input_tokens", 0) or 0
    prompt = cached + fresh
    return {
        "trace_id": doc.get("trace_id"),
        "timestamp": doc.get("timestamp"),
        "caller": doc.get("caller"),
        "model": doc.get("model"),
        "conversation_id": doc.get("conversation_id"),
        "session_id": doc.get("session_id"),
        "path": metadata.get("path"),
        "status_code": metadata.get("status_code"),
        "outcome": metadata.get("outcome"),
        "route_reason": metadata.get("route_reason"),
        "streamed": metadata.get("streamed", False),
        "latency_ms": metadata.get("latency_ms"),
        "routing_ms": metadata.get("routing_ms"),
        "queue_wait_ms": metadata.get("queue_wait_ms"),
        "backend_ms": metadata.get("backend_ms"),
        "first_chunk_ms": metadata.get("first_chunk_ms"),
        "prompt_tokens": prompt,
        "fresh_prompt_tokens": fresh,
        "cache_read_tokens": cached,
        "cache_hit_rate": round(cached / prompt, 4) if prompt else 0.0,
        "output_tokens": doc.get("output_tokens", 0) or 0,
        "context_tokens": metadata.get("context_tokens"),
        "prompt_tokens_per_second": metadata.get("prompt_tokens_per_second"),
        "decode_tokens_per_second": metadata.get("decode_tokens_per_second"),
        "speculative_draft_tokens": metadata.get("speculative_draft_tokens"),
        "speculative_accepted_tokens": metadata.get("speculative_accepted_tokens"),
        "speculative_acceptance_rate": metadata.get("speculative_acceptance_rate"),
        "preamble": {
            "state": preamble.get("state"),
            "change_reason": preamble.get("change_reason"),
            "fingerprint": preamble.get("fingerprint"),
            "previous_fingerprint": preamble.get("previous_fingerprint"),
            "prefix_bytes": preamble.get("prefix_bytes"),
            "system_bytes": preamble.get("system_bytes"),
            "tools_bytes": preamble.get("tools_bytes"),
            "tool_count": preamble.get("tool_count"),
        },
    }


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
                "cache_read_tokens": {"$sum": "$cache_read_tokens"},
                "cache_write_tokens": {"$sum": "$cache_write_tokens"},
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
        cache_read = r.get("cache_read_tokens", 0) or 0
        denom = cache_read + (r.get("input_tokens", 0) or 0)
        r["cache_hit_rate"] = round(cache_read / denom, 4) if denom else 0.0
    return rows


@router.get("/usage/by-caller")
async def usage_by_caller(
    days: int = 7,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Gateway token and cache totals grouped by declared/fallback caller."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pipeline = [
        {
            "$match": {
                "timestamp": {"$gte": cutoff},
                "caller": {"$nin": [None, ""]},
            }
        },
        {
            "$group": {
                "_id": "$caller",
                "backend": {"$first": "$backend"},
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
                "total_tokens": {"$sum": "$total_tokens"},
                "cache_read_tokens": {"$sum": "$cache_read_tokens"},
                "requests": {"$sum": 1},
            }
        },
        {"$sort": {"total_tokens": -1}},
    ]
    rows = await db.usage.aggregate(pipeline).to_list(length=200)
    for row in rows:
        cached = row.get("cache_read_tokens", 0) or 0
        fresh = row.get("input_tokens", 0) or 0
        row["cache_hit_rate"] = round(cached / (cached + fresh), 4) if cached + fresh else 0.0
    return rows


@router.get("/usage/traces")
async def usage_traces(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=50, ge=1, le=200),
    caller: str | None = Query(default=None, max_length=120),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Recent content-free inference timelines from the OpenAI gateway."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    query: dict = {
        "source": "llm-gateway",
        "trace_id": {"$nin": [None, ""]},
        "timestamp": {"$gte": cutoff},
    }
    if caller:
        query["caller"] = caller
    cursor = db.usage.find(query).sort("timestamp", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [_inference_trace_row(doc) for doc in docs]


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
