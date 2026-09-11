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
        cache_read_tokens: Optional[int] = 0,
        cache_write_tokens: int = 0,
        cache_reported: Optional[bool] = None,
        agent_slug: Optional[str] = None,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        caller: Optional[str] = None,
        trace_id: Optional[str] = None,
        preamble_hash: Optional[str] = None,
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
            # Prompt-cache accounting. The cache-hit rate is
            # cache_read / (cache_read + input) — see summary().
            #
            # `cache_reported=False` means the BACKEND does not report reuse,
            # which is not the same as reusing nothing. Rows marked that way are
            # excluded from every hit-rate denominator: Red's Radiance answers
            # with a null prompt_tokens_details, and counting it as 0% made a
            # backend with a measurably working prefix cache (5.2x cold-to-warm)
            # read as having none. Legacy rows have no flag and are treated as
            # reporting, which is what they were.
            "cache_read_tokens": cache_read_tokens if cache_reported is not False else None,
            "cache_write_tokens": cache_write_tokens or 0,
            "cache_reported": (
                cache_reported if cache_reported is not None else cache_read_tokens is not None
            ),
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
        # Omit absent values rather than storing null: the trace id has a
        # sparse unique index, and MongoDB sparse indexes still index an
        # explicitly present null value. These are opaque ids/hashes only.
        if trace_id:
            doc["trace_id"] = trace_id
        if preamble_hash:
            doc["preamble_hash"] = preamble_hash
        result = await self.db.usage.insert_one(doc)
        return str(result.inserted_id)

    # A row counts toward the cache-hit denominator only if its backend
    # actually reports reuse. Legacy rows predate the flag and were all written
    # by reporting backends, so a MISSING field must read as reporting — hence
    # `$ne … False` rather than a truthiness test.
    REPORTED_CACHE = {"$ne": ["$cache_reported", False]}

    #: `$group` fields every cache-aware aggregation needs.
    CACHE_GROUP_FIELDS = {
        "cache_read_tokens": {"$sum": "$cache_read_tokens"},
        "reported_input_tokens": {
            "$sum": {"$cond": [REPORTED_CACHE, "$input_tokens", 0]}
        },
        "reported_requests": {"$sum": {"$cond": [REPORTED_CACHE, 1, 0]}},
    }

    @staticmethod
    def _hit_rate(cache_read: int, input_tokens: int) -> float:
        """Weighted cache-hit rate: cached prompt tokens as a share of all prompt
        tokens (cache_read + fresh input). Matches Pi-Flow's cacheHitRate."""
        denom = (cache_read or 0) + (input_tokens or 0)
        return round((cache_read or 0) / denom, 4) if denom else 0.0

    @classmethod
    def annotate_cache(cls, row: dict) -> dict:
        """Add `cache_hit_rate` and `cache_reporting` to a grouped row.

        The rate is computed over reporting requests ONLY. When nothing in the
        group reports, the rate is None — the honest answer to "how much did it
        reuse?" from a backend that never said. Reporting 0.0 there is what let
        33M prompt tokens on a working prefix cache read as no reuse at all.
        """
        cache_read = row.get("cache_read_tokens") or 0
        reported_requests = row.get("reported_requests")
        requests = row.get("requests")
        if reported_requests == 0:
            row["cache_hit_rate"] = None
            row["cache_reporting"] = "unsupported"
            return row
        # A row assembled without the reporting fields (an older pipeline, or a
        # caller building rows by hand) says nothing about which backends
        # answered. Fall back to the whole input rather than to zero: reading a
        # missing breakdown as "none of it reported" would invert the fix and
        # turn every such group into a 100% hit rate.
        reported_input = row.get("reported_input_tokens")
        if reported_input is None:
            reported_input = row.get("input_tokens") or 0
        denom = cache_read + reported_input
        row["cache_hit_rate"] = round(cache_read / denom, 4) if denom else 0.0
        row["cache_reporting"] = (
            "partial"
            if reported_requests is not None and requests is not None and reported_requests < requests
            else "reported"
        )
        return row

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

    #: Bucket granularities `series()` accepts, and the $dateTrunc unit each maps to.
    SERIES_BUCKETS = {"hour": "hour", "day": "day"}

    #: What `series()` may group a bucket's tokens by. `none` returns bucket totals only.
    SERIES_DIMENSIONS = {
        "model": "$model",
        "caller": "$caller",
        "agent": "$agent_slug",
        "none": None,
    }

    async def series(
        self,
        *,
        days: int = 30,
        bucket: str = "day",
        by: str = "model",
        top: int = 4,
    ) -> dict:
        """Token totals over time, bucketed, optionally split by one dimension.

        The usage collection already carries a year of per-request rows on the
        `usage_timestamp` TTL index, which doubles as the range index this
        aggregation uses -- so the history is a `$group`, not a new collection.

        Two things this deliberately does NOT do:

        * It never invents a bucket. Only buckets that contain requests are
          returned, and the caller decides whether a missing bucket is a gap or
          a zero. For tokens it is a zero; for `cache_hit_rate` it is a GAP, and
          conflating them is exactly the failure `cache_reporting` exists to
          prevent.
        * It never reports a cache-hit rate a backend did not supply.
          `annotate_cache` is applied per bucket, so a bucket served only by
          non-reporting backends returns `cache_hit_rate: None` /
          `cache_reporting: "unsupported"` rather than a fabricated 0%.

        `top` folds everything past the N largest series into a single `Other`,
        because a chart with one colour per model stops being readable long
        before the registry stops adding models.
        """
        unit = self.SERIES_BUCKETS.get(bucket)
        if unit is None:
            raise ValueError(f"unknown bucket {bucket!r}")
        if by not in self.SERIES_DIMENSIONS:
            raise ValueError(f"unknown dimension {by!r}")
        field = self.SERIES_DIMENSIONS[by]

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        group_id: dict = {"t": {"$dateTrunc": {"date": "$timestamp", "unit": unit}}}
        if field is not None:
            group_id["k"] = field

        pipeline = [
            {"$match": {"timestamp": {"$gte": cutoff}}},
            {"$group": {
                "_id": group_id,
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
                "total_tokens": {"$sum": "$total_tokens"},
                "requests": {"$sum": 1},
                **self.CACHE_GROUP_FIELDS,
            }},
            {"$sort": {"_id.t": 1}},
        ]
        rows = await self.db.usage.aggregate(pipeline).to_list(length=20000)

        # Rank the dimension over the WHOLE window, not per bucket: a series
        # must keep its colour when it is small, or the legend lies.
        totals: dict[str, int] = {}
        for r in rows:
            key = self._series_key(r, field)
            totals[key] = totals.get(key, 0) + (r.get("total_tokens") or 0)
        ranked = sorted(totals, key=lambda k: -totals[k])
        keep = ranked[:top] if field is not None else ranked
        folded = set(ranked[top:]) if field is not None else set()

        buckets: dict = {}
        for r in rows:
            when = (r.get("_id") or {}).get("t")
            slot = buckets.setdefault(when, {
                "t": when,
                "total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
                "requests": 0, "cache_read_tokens": 0,
                "reported_input_tokens": 0, "reported_requests": 0,
                "by": {},
            })
            for f in ("total_tokens", "input_tokens", "output_tokens", "requests",
                      "cache_read_tokens", "reported_input_tokens", "reported_requests"):
                slot[f] += r.get(f) or 0
            if field is not None:
                key = self._series_key(r, field)
                name = "Other" if key in folded else key
                slot["by"][name] = slot["by"].get(name, 0) + (r.get("total_tokens") or 0)

        out = []
        for slot in sorted(buckets.values(), key=lambda s: s["t"] or datetime.min):
            self.annotate_cache(slot)
            # The reporting breakdown is an implementation detail of the rate.
            for f in ("reported_input_tokens", "reported_requests"):
                slot.pop(f, None)
            out.append(slot)

        return {
            "days": days,
            "bucket": bucket,
            "by": by,
            "series": ([*keep, "Other"] if folded else list(keep)) if field is not None else [],
            "buckets": out,
        }

    @staticmethod
    def _series_key(row: dict, field: str | None) -> str:
        """The dimension value for one grouped row, with absences named.

        `agent_slug` and `caller` are frequently absent -- a gateway request
        from a non-ARIA client has no agent, and that is not the same fact as a
        request from an agent called "unknown". Name it `unattributed` so the
        chart says which it is.
        """
        if field is None:
            return "all"
        value = (row.get("_id") or {}).get("k")
        if value in (None, ""):
            return "unattributed"
        return str(value)

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
                "cache_write_tokens": {"$sum": "$cache_write_tokens"},
                "requests": {"$sum": 1},
                **self.CACHE_GROUP_FIELDS,
            }},
            {"$sort": {"total_tokens": -1}},
        ]
        rows = await self.db.usage.aggregate(pipeline).to_list(length=500)
        for r in rows:
            self.annotate_cache(r)
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
                    "cache_write_tokens": {"$sum": "$cache_write_tokens"},
                    "requests": {"$sum": 1},
                    **self.CACHE_GROUP_FIELDS,
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
                "cache_reporting": "reported",
                "requests": 0,
            }
        row = result[0]
        row["cache_read_tokens"] = row.get("cache_read_tokens", 0)
        row["cache_write_tokens"] = row.get("cache_write_tokens", 0)
        return self.annotate_cache(row)
