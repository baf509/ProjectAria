"""
ARIA - Usage Repository

Purpose: Persist model usage metadata for later aggregation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.llm.pricing import cost_for


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
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        agent_slug: Optional[str] = None,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        caller: Optional[str] = None,
        backend: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        metadata = metadata or {}
        # Store backend top-level so usage can be priced (local backends are
        # free; cloud ones aren't). Fall back to the backend in metadata.
        backend = backend or metadata.get("backend")
        doc = {
            "model": model,
            "source": source,
            "backend": backend,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            # Prompt-cache accounting (0 for backends without caching). The
            # cache-hit rate is cache_read / (cache_read + input) — see summary().
            "cache_read_tokens": cache_read_tokens or 0,
            "cache_write_tokens": cache_write_tokens or 0,
            "agent_slug": agent_slug,
            "conversation_id": conversation_id,
            "session_id": session_id,
            # The gateway has callers that are not ARIA agents/conversations
            # (Hermes, Pi, eval harnesses). Keep their declared/fallback
            # identity queryable without overloading agent_slug.
            "caller": caller,
            "metadata": metadata,
            "timestamp": datetime.now(timezone.utc),
        }
        result = await self.db.usage.insert_one(doc)
        return str(result.inserted_id)

    @staticmethod
    def _hit_rate(cache_read: int, input_tokens: int) -> float:
        """Weighted cache-hit rate: cached prompt tokens as a share of all prompt
        tokens (cache_read + fresh input). Matches Pi-Flow's cacheHitRate."""
        denom = (cache_read or 0) + (input_tokens or 0)
        return round((cache_read or 0) / denom, 4) if denom else 0.0

    @staticmethod
    def _price_rows(rows: list[dict]) -> list[dict]:
        """Annotate (model, backend)-grouped rows with a `cost` field."""
        for r in rows:
            gid = r.get("_id") or {}
            model = gid.get("model") if isinstance(gid, dict) else gid
            backend = gid.get("backend") if isinstance(gid, dict) else None
            r["cost"] = round(
                cost_for(model, r.get("input_tokens", 0), r.get("output_tokens", 0), backend),
                6,
            )
        return rows

    async def by_model_cost(self, days: int = 7) -> list[dict]:
        """Token + cost totals grouped by (model, backend)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        pipeline = [
            {"$match": {"timestamp": {"$gte": cutoff}}},
            {"$group": {
                "_id": {"model": "$model", "backend": "$backend"},
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
                "total_tokens": {"$sum": "$total_tokens"},
                "cache_read_tokens": {"$sum": "$cache_read_tokens"},
                "cache_write_tokens": {"$sum": "$cache_write_tokens"},
                "requests": {"$sum": 1},
            }},
            {"$sort": {"total_tokens": -1}},
        ]
        rows = await self.db.usage.aggregate(pipeline).to_list(length=500)
        for r in rows:
            r["cache_hit_rate"] = self._hit_rate(
                r.get("cache_read_tokens", 0), r.get("input_tokens", 0)
            )
        return self._price_rows(rows)

    async def cost_summary(self, days: int = 7) -> dict:
        """Total $ cost over the window plus a per-(model,backend) breakdown."""
        rows = await self.by_model_cost(days=days)
        total = round(sum(r["cost"] for r in rows), 6)
        return {"days": days, "total_cost": total, "by_model": rows}

    async def cost_since(self, cutoff: datetime) -> float:
        """Total $ cost of usage recorded since `cutoff` (for the spend cap)."""
        pipeline = [
            {"$match": {"timestamp": {"$gte": cutoff}}},
            {"$group": {
                "_id": {"model": "$model", "backend": "$backend"},
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
            }},
        ]
        rows = self._price_rows(await self.db.usage.aggregate(pipeline).to_list(length=500))
        return round(sum(r["cost"] for r in rows), 6)

    async def cost_for_conversations(
        self, conversation_ids: list[str], days: int = 30
    ) -> dict[str, dict]:
        """Token + cost totals for MANY conversations in one aggregation.

        The project cockpit priced up to 25 sessions with 25 sequential
        cost_for_conversation() calls. Same math, same `usage_timestamp`
        index, one round-trip. Conversations with no usage rows are absent
        from the result -- callers treat that as "nothing priced", which is
        what the per-conversation version returned as zeros.
        """
        out: dict[str, dict] = {}
        ids = [c for c in dict.fromkeys(conversation_ids) if c]
        if not ids:
            return out
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        pipeline = [
            {"$match": {"conversation_id": {"$in": ids}, "timestamp": {"$gte": cutoff}}},
            {"$group": {
                "_id": {"c": "$conversation_id", "model": "$model", "backend": "$backend"},
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
                "total_tokens": {"$sum": "$total_tokens"},
                "requests": {"$sum": 1},
            }},
        ]
        rows = self._price_rows(
            await self.db.usage.aggregate(pipeline).to_list(length=2000)
        )
        for r in rows:
            conv = (r.get("_id") or {}).get("c")
            if not conv:
                continue
            agg = out.setdefault(conv, {
                "conversation_id": conv,
                "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "requests": 0, "cost": 0.0,
            })
            agg["input_tokens"] += r.get("input_tokens", 0)
            agg["output_tokens"] += r.get("output_tokens", 0)
            agg["total_tokens"] += r.get("total_tokens", 0)
            agg["requests"] += r.get("requests", 0)
            agg["cost"] = round(agg["cost"] + r["cost"], 6)
        return out

    async def cost_for_conversation(self, conversation_id: str, days: int = 30) -> dict:
        """Token + cost totals for one conversation (used for per-session cost)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        pipeline = [
            {"$match": {"conversation_id": conversation_id, "timestamp": {"$gte": cutoff}}},
            {"$group": {
                "_id": {"model": "$model", "backend": "$backend"},
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
                "total_tokens": {"$sum": "$total_tokens"},
                "requests": {"$sum": 1},
            }},
        ]
        rows = self._price_rows(await self.db.usage.aggregate(pipeline).to_list(length=200))
        return {
            "conversation_id": conversation_id,
            "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
            "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
            "total_tokens": sum(r.get("total_tokens", 0) for r in rows),
            "requests": sum(r.get("requests", 0) for r in rows),
            "cost": round(sum(r["cost"] for r in rows), 6),
        }

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
                    "cache_read_tokens": {"$sum": "$cache_read_tokens"},
                    "cache_write_tokens": {"$sum": "$cache_write_tokens"},
                    "requests": {"$sum": 1},
                }
            },
        ]
        result = await self.db.usage.aggregate(pipeline).to_list(length=1)
        if not result:
            return {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cache_hit_rate": 0.0,
                "requests": 0,
            }
        row = result[0]
        row["cache_read_tokens"] = row.get("cache_read_tokens", 0)
        row["cache_write_tokens"] = row.get("cache_write_tokens", 0)
        row["cache_hit_rate"] = self._hit_rate(
            row["cache_read_tokens"], row.get("input_tokens", 0)
        )
        return row
