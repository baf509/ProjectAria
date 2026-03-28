"""
ARIA - Memories Routes

Phase: 2
Purpose: Memory CRUD and search operations

Related Spec Sections:
- Section 5.1: REST Endpoints (Memories)
"""

from datetime import datetime, timedelta, timezone
import json
from typing import Optional
from aria.api.deps import valid_object_id
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from aria.api.deps import get_db, get_task_runner
from aria.memory.long_term import LongTermMemory
from aria.memory.extraction import MemoryExtractor
from aria.tasks.runner import TaskRunner


router = APIRouter()


# Request/Response Models
class MemoryCreate(BaseModel):
    """Request to create a memory."""

    content: str
    content_type: str  # "fact" | "preference" | "event" | "skill" | "document"
    categories: Optional[list[str]] = []
    importance: float = 0.5


class MemoryUpdate(BaseModel):
    """Request to update a memory."""

    content: Optional[str] = None
    content_type: Optional[str] = None
    categories: Optional[list[str]] = None
    importance: Optional[float] = None
    verified: Optional[bool] = None


class MemorySearch(BaseModel):
    """Request to search memories."""

    query: str
    limit: int = 10
    content_type: Optional[str] = None
    categories: Optional[list[str]] = None


class MemoryResponse(BaseModel):
    """Memory response."""

    id: str
    content: str
    content_type: str
    categories: list[str]
    importance: float
    confidence: Optional[float]
    verified: bool
    created_at: datetime
    source: dict
    access_count: int = 0


class MemoryImportRequest(BaseModel):
    """Import memories from a payload."""

    memories: list[MemoryCreate]


class MemoryMaintenanceRequest(BaseModel):
    """Apply confidence decay and archival to stale memories."""

    older_than_days: int = 90
    min_access_count: int = 1
    confidence_decay: float = 0.05


def _serialize_memory_doc(doc: dict) -> MemoryResponse:
    doc["id"] = str(doc.pop("_id"))
    doc.pop("embedding", None)
    doc.pop("embedding_model", None)
    doc.setdefault("access_count", 0)
    return MemoryResponse(**doc)


@router.get("/memories", response_model=list[MemoryResponse])
async def list_memories(
    limit: int = 50,
    skip: int = 0,
    content_type: Optional[str] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List memories."""
    query_filter = {"status": "active"}
    if content_type:
        query_filter["content_type"] = content_type

    cursor = (
        db.memories.find(query_filter)
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )

    memories = []
    async for doc in cursor:
        memories.append(_serialize_memory_doc(doc))

    return memories


@router.post("/memories", response_model=MemoryResponse, status_code=201)
async def create_memory(
    body: MemoryCreate, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """Create a new memory manually."""
    long_term = LongTermMemory(db)

    memory_id = await long_term.create_memory(
        content=body.content,
        content_type=body.content_type,
        categories=body.categories,
        importance=body.importance,
        confidence=1.0,  # Manual entry = high confidence
        source={"type": "manual", "created_at": datetime.now(timezone.utc)},
    )

    # Fetch created memory
    memory_doc = await db.memories.find_one({"_id": valid_object_id(memory_id)})
    return _serialize_memory_doc(memory_doc)


@router.get("/memories/export")
async def export_memories(
    format: str = "json",
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Export all active memories as JSON or markdown."""
    docs = await db.memories.find({"status": "active"}).sort("created_at", -1).to_list(length=None)
    memories = [_serialize_memory_doc(doc).model_dump(mode="json") for doc in docs]

    if format == "markdown":
        lines = ["# Memories", ""]
        for memory in memories:
            lines.extend(
                [
                    f"## {memory['content_type']}: {memory['content']}",
                    f"- Categories: {', '.join(memory.get('categories', [])) or 'none'}",
                    f"- Importance: {memory['importance']}",
                    f"- Confidence: {memory.get('confidence', 'n/a')}",
                    f"- Created: {memory['created_at']}",
                    "",
                ]
            )
        return {"format": "markdown", "content": "\n".join(lines)}

    return {"format": "json", "memories": memories}


@router.get("/memories/{memory_id}", response_model=MemoryResponse)
async def get_memory(memory_id: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Get a memory by ID."""
    memory_doc = await db.memories.find_one({"_id": valid_object_id(memory_id)})

    if not memory_doc:
        raise HTTPException(status_code=404, detail="Memory not found")

    return _serialize_memory_doc(memory_doc)


@router.patch("/memories/{memory_id}", response_model=MemoryResponse)
async def update_memory(
    memory_id: str,
    body: MemoryUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Update a memory."""
    long_term = LongTermMemory(db)

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    success = await long_term.update_memory(memory_id, update_data)

    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Fetch updated memory
    memory_doc = await db.memories.find_one({"_id": valid_object_id(memory_id)})
    return _serialize_memory_doc(memory_doc)


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: str, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """Delete a memory (soft delete)."""
    long_term = LongTermMemory(db)

    success = await long_term.delete_memory(memory_id)

    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")


@router.post("/memories/search", response_model=list[MemoryResponse])
async def search_memories(
    body: MemorySearch, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """Search memories using hybrid search."""
    long_term = LongTermMemory(db)

    # Build filters
    filters = {}
    if body.content_type:
        filters["content_type"] = body.content_type
    if body.categories:
        filters["categories"] = {"$in": body.categories}

    # Search
    memories = await long_term.search(
        query=body.query, limit=body.limit, filters=filters if filters else None
    )

    # Convert to response format
    results = []
    for memory in memories:
        memory_dict = memory.to_dict()
        # Fetch full document for access_count
        doc = await db.memories.find_one({"_id": valid_object_id(memory.id)})
        memory_dict["access_count"] = doc.get("access_count", 0) if doc else 0
        results.append(MemoryResponse(**memory_dict))

    return results


@router.post("/memories/extract/{conversation_id}", status_code=202)
async def extract_memories(
    conversation_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    task_runner: TaskRunner = Depends(get_task_runner),
):
    """
    Extract memories from a conversation (async background task).
    Returns immediately, extraction happens in background.
    """
    # Verify conversation exists
    conversation = await db.conversations.find_one(
        {"_id": valid_object_id(conversation_id)}
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Get agent's LLM configuration
    agent = await db.agents.find_one(
        {"_id": valid_object_id(conversation["agent_id"])}
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    llm_config = agent.get("llm", {})
    llm_backend = llm_config.get("backend", "llamacpp")
    llm_model = llm_config.get("model", "default")

    async def run_extraction():
        extractor = MemoryExtractor(db)
        count = await extractor.extract_from_conversation(
            conversation_id,
            llm_backend=llm_backend,
            llm_model=llm_model
        )
        return {
            "conversation_id": conversation_id,
            "extracted_count": count,
        }

    task_id = await task_runner.submit_task(
        name="memory_extraction",
        coroutine_factory=run_extraction,
        notify=True,
        metadata={"conversation_id": conversation_id, "task_kind": "memory_extraction"},
    )

    return {
        "message": "Memory extraction started",
        "conversation_id": conversation_id,
        "task_id": task_id,
    }


@router.post("/memories/import")
async def import_memories(
    body: MemoryImportRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Import memories with simple content-based deduplication."""
    long_term = LongTermMemory(db)
    imported = 0
    skipped = 0

    for memory in body.memories:
        existing = await db.memories.find_one(
            {"content": memory.content, "status": "active"}
        )
        if existing:
            skipped += 1
            continue

        await long_term.create_memory(
            content=memory.content,
            content_type=memory.content_type,
            categories=memory.categories,
            importance=memory.importance,
            confidence=0.9,
            source={"type": "import", "imported_at": datetime.now(timezone.utc)},
        )
        imported += 1

    return {"imported": imported, "skipped": skipped}


@router.post("/memories/maintenance")
async def run_memory_maintenance(
    body: MemoryMaintenanceRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Decay confidence and archive stale, low-value memories."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=body.older_than_days)
    docs = await db.memories.find({"status": "active"}).to_list(length=None)

    decayed = 0
    archived = 0
    for doc in docs:
        created_at = doc.get("created_at")
        if not created_at:
            continue
        # Ensure timezone-aware comparison
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        if created_at < cutoff:
            next_confidence = max(0.0, float(doc.get("confidence", 0.5)) - body.confidence_decay)
            await db.memories.update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "confidence": next_confidence,
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            decayed += 1

            if next_confidence < 0.35 and int(doc.get("access_count", 0)) <= body.min_access_count:
                await db.memories.update_one(
                    {"_id": doc["_id"]},
                    {
                        "$set": {
                            "status": "archived",
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                )
                archived += 1

    return {"decayed": decayed, "archived": archived}
