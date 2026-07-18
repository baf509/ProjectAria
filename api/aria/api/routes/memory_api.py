"""
ARIA - Shared Services · S1: Memory HTTP API

Phase: Shared Services (SHARED_SERVICES_DESIGN.md · S1)
Purpose: Minimal, stable cross-machine memory surface — recall + store — wrapping
         the existing LongTermMemory. Embeds server-side so thin stdlib clients on
         other machines (macbook, Red, Ridge) need no venv or embedding service.

These endpoints inherit the global X-API-Key auth (api_key_middleware in main.py);
the portable client carries the key in its config. See S4.
"""
import logging
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_db
from aria.memory.long_term import LongTermMemory

logger = logging.getLogger(__name__)

router = APIRouter()


class RecallRequest(BaseModel):
    query: str
    k: int = Field(default=10, ge=1, le=100)
    content_type: Optional[str] = None
    categories: Optional[list[str]] = None


class StoreRequest(BaseModel):
    content: str
    type: str = "fact"  # fact | preference | event | skill | document | decision | plan
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    categories: Optional[list[str]] = None
    confidence: Optional[float] = None
    private: bool = False
    source: Optional[dict] = None


@router.post("/memory/recall")
async def memory_recall(body: RecallRequest, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Semantic recall over aria.memories (hybrid vector + lexical, embedded server-side)."""
    ltm = LongTermMemory(db)
    filters: dict = {}
    if body.content_type:
        filters["content_type"] = body.content_type
    if body.categories:
        filters["categories"] = {"$in": body.categories}
    results = await ltm.search(body.query, limit=body.k, filters=filters or None)
    # Memories may carry ObjectId/datetime inside source — jsonable_encoder makes
    # the whole payload JSON-safe.
    return jsonable_encoder(
        {"query": body.query, "count": len(results), "results": [m.to_dict() for m in results]},
        custom_encoder={ObjectId: str},
    )


@router.post("/memory/store", status_code=201)
async def memory_store(body: StoreRequest, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Store a single fact (embedded + deduped server-side). Write — requires the API key."""
    ltm = LongTermMemory(db)
    source = body.source or {"type": "memory_api"}
    memory_id = await ltm.create_memory(
        content=body.content,
        content_type=body.type,
        categories=body.categories or [],
        importance=body.importance,
        confidence=body.confidence,
        source=source,
        private=body.private,
    )
    return {"id": memory_id}
