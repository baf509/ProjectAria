"""
ARIA - Memories Routes

Phase: 2
Purpose: Memory CRUD and search operations

Related Spec Sections:
- Section 5.1: REST Endpoints (Memories)
"""

from datetime import datetime
from typing import Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from aria.api.deps import get_db
from aria.memory.long_term import LongTermMemory
from aria.memory.extraction import MemoryExtractor


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
        doc["id"] = str(doc.pop("_id"))
        # Remove embedding from response (too large)
        doc.pop("embedding", None)
        doc.pop("embedding_model", None)
        doc.setdefault("access_count", 0)
        memories.append(MemoryResponse(**doc))

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
        source={"type": "manual", "created_at": datetime.utcnow()},
    )

    # Fetch created memory
    memory_doc = await db.memories.find_one({"_id": ObjectId(memory_id)})
    memory_doc["id"] = str(memory_doc.pop("_id"))
    memory_doc.pop("embedding", None)
    memory_doc.pop("embedding_model", None)
    memory_doc.setdefault("access_count", 0)

    return MemoryResponse(**memory_doc)


@router.get("/memories/{memory_id}", response_model=MemoryResponse)
async def get_memory(memory_id: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Get a memory by ID."""
    memory_doc = await db.memories.find_one({"_id": ObjectId(memory_id)})

    if not memory_doc:
        raise HTTPException(status_code=404, detail="Memory not found")

    memory_doc["id"] = str(memory_doc.pop("_id"))
    memory_doc.pop("embedding", None)
    memory_doc.pop("embedding_model", None)
    memory_doc.setdefault("access_count", 0)

    return MemoryResponse(**memory_doc)


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
    memory_doc = await db.memories.find_one({"_id": ObjectId(memory_id)})
    memory_doc["id"] = str(memory_doc.pop("_id"))
    memory_doc.pop("embedding", None)
    memory_doc.pop("embedding_model", None)
    memory_doc.setdefault("access_count", 0)

    return MemoryResponse(**memory_doc)


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
        doc = await db.memories.find_one({"_id": ObjectId(memory.id)})
        memory_dict["access_count"] = doc.get("access_count", 0) if doc else 0
        results.append(MemoryResponse(**memory_dict))

    return results


@router.post("/memories/extract/{conversation_id}", status_code=202)
async def extract_memories(
    conversation_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Extract memories from a conversation (async background task).
    Returns immediately, extraction happens in background.
    """
    # Verify conversation exists
    conversation = await db.conversations.find_one(
        {"_id": ObjectId(conversation_id)}
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Get agent's LLM configuration
    agent = await db.agents.find_one(
        {"_id": ObjectId(conversation["agent_id"])}
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    llm_config = agent.get("llm", {})
    llm_backend = llm_config.get("backend", "ollama")
    llm_model = llm_config.get("model", "llama3.2:latest")

    # Schedule extraction as background task
    async def run_extraction():
        extractor = MemoryExtractor(db)
        count = await extractor.extract_from_conversation(
            conversation_id,
            llm_backend=llm_backend,
            llm_model=llm_model
        )
        print(f"Extracted {count} memories from conversation {conversation_id}")

    background_tasks.add_task(run_extraction)

    return {
        "message": "Memory extraction started",
        "conversation_id": conversation_id,
    }
