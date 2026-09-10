"""Close condition alerts when their producer has observed recovery."""

from datetime import datetime


async def resolve_alerts(db, *, source: str, event_type: str, observed_at: datetime) -> int:
    """Preserve history and human decisions; only close older, open incidents.

    The timestamp guard keeps a delayed recovery from closing a newer failure.
    Calling this on healthy checks also repairs rows left open across restarts.
    """
    result = await db.alerts.update_many(
        {
            "source": source,
            "event_type": event_type,
            "acked": False,
            "$or": [
                {"last_seen_at": {"$lte": observed_at}},
                {"last_seen_at": None, "created_at": {"$lte": observed_at}},
            ],
        },
        {"$set": {
            "acked": True,
            "acked_at": observed_at,
            "needs_human": False,
            "resolution": {
                "by": source,
                "at": observed_at,
                "reason": "Producer observed recovery",
            },
        }},
    )
    return result.modified_count
