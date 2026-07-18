"""
ARIA - Shared Services routes (SHARED_SERVICES_DESIGN.md)

S3 review surface: the glanceable "needs-review" list the scan/reconcile worker
writes (removed services, curated-vs-observed contradictions). Inherits global
X-API-Key auth.
"""
from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.api.deps import get_db
from aria.shared.review import ack_review_item, list_review_items

router = APIRouter()


@router.get("/shared/review")
async def get_review(
    unacked_only: bool = True,
    limit: int = 50,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    items = await list_review_items(db, unacked_only=unacked_only, limit=limit)
    return {"count": len(items), "items": items}


@router.post("/shared/review/{item_id}/ack")
async def ack_review(item_id: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    return {"acked": await ack_review_item(db, item_id)}
