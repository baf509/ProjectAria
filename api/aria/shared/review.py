"""
ARIA - Shared Services · S3: Review surface ("needs-review" list)

The only recurring human task. The scan/reconcile worker writes glanceable,
ignorable review items here (contradictions between curated notes and live state,
removed services, new unclaimed services). Surfaced via GET /api/v1/shared/review.
Nothing breaks if the list is ignored — the structural map still self-updates.
"""
import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

COLLECTION = "scan_review"


async def add_review_item(
    db: AsyncIOMotorDatabase,
    *,
    kind: str,
    detail: str,
    subject: str | None = None,
    source: str = "scan-worker",
) -> None:
    """Upsert a review item, deduped by (kind, subject) while still unacked."""
    now = datetime.now(timezone.utc)
    key = {"kind": kind, "subject": subject, "acked": False}
    await db[COLLECTION].update_one(
        key,
        {
            "$set": {"detail": detail, "source": source, "updated_at": now},
            "$setOnInsert": {"created_at": now, "acked": False},
        },
        upsert=True,
    )


async def list_review_items(
    db: AsyncIOMotorDatabase, *, unacked_only: bool = True, limit: int = 50
) -> list[dict]:
    q = {"acked": False} if unacked_only else {}
    docs = await db[COLLECTION].find(q).sort("created_at", -1).to_list(length=limit)
    for d in docs:
        d["id"] = str(d.pop("_id"))
    return docs


async def ack_review_item(db: AsyncIOMotorDatabase, item_id: str) -> bool:
    from bson import ObjectId

    res = await db[COLLECTION].update_one(
        {"_id": ObjectId(item_id)},
        {"$set": {"acked": True, "acked_at": datetime.now(timezone.utc)}},
    )
    return res.modified_count > 0
