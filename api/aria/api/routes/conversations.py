"""
ARIA - Conversations Routes

Phase: 1
Purpose: Conversation CRUD and message handling

Related Spec Sections:
- Section 5.1: REST Endpoints
- Section 5.3: SSE Stream Format
"""

import json
from datetime import datetime
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from motor.motor_asyncio import AsyncIOMotorDatabase
from sse_starlette.sse import EventSourceResponse

from aria.api.deps import get_db, get_orchestrator
from aria.db.models import (
    ConversationCreate,
    ConversationResponse,
    ConversationListItem,
    ConversationUpdate,
    MessageRequest,
    Message,
    LLMConfig,
    ConversationStats,
)
from aria.core.orchestrator import Orchestrator

router = APIRouter()


def serialize_conversation(doc: dict) -> dict:
    """Convert MongoDB document to API response."""
    doc["id"] = str(doc.pop("_id"))
    doc["agent_id"] = str(doc["agent_id"])
    return doc


@router.get("/conversations", response_model=list[ConversationListItem])
async def list_conversations(
    limit: int = 50,
    skip: int = 0,
    status: str = "active",
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List conversations."""
    cursor = (
        db.conversations.find({"status": status})
        .sort("updated_at", -1)
        .skip(skip)
        .limit(limit)
    )

    conversations = []
    async for doc in cursor:
        # Remove messages for list view
        doc.pop("messages", None)
        conversations.append(ConversationListItem(**serialize_conversation(doc)))

    return conversations


@router.post("/conversations", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    body: ConversationCreate, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """Create a new conversation."""
    # Get agent (use default if not specified)
    if body.agent_id:
        agent = await db.agents.find_one({"_id": ObjectId(body.agent_id)})
    elif body.agent_slug:
        agent = await db.agents.find_one({"slug": body.agent_slug})
    else:
        agent = await db.agents.find_one({"is_default": True})

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Create conversation document
    now = datetime.utcnow()
    conversation = {
        "agent_id": agent["_id"],
        "title": body.title or "New Conversation",
        "summary": None,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "llm_config": {
            "backend": agent["llm"]["backend"],
            "model": agent["llm"]["model"],
            "temperature": agent["llm"]["temperature"],
        },
        "messages": [],
        "tags": [],
        "pinned": False,
        "stats": {"message_count": 0, "total_tokens": 0, "tool_calls": 0},
    }

    result = await db.conversations.insert_one(conversation)
    conversation["_id"] = result.inserted_id

    return ConversationResponse(**serialize_conversation(conversation))


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """Get a conversation with all messages."""
    conversation = await db.conversations.find_one({"_id": ObjectId(conversation_id)})
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return ConversationResponse(**serialize_conversation(conversation))


@router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: str,
    body: ConversationUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Update conversation metadata."""
    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    update_data["updated_at"] = datetime.utcnow()

    result = await db.conversations.update_one(
        {"_id": ObjectId(conversation_id)}, {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Conversation not found")

    conversation = await db.conversations.find_one({"_id": ObjectId(conversation_id)})
    return ConversationResponse(**serialize_conversation(conversation))


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """Delete a conversation."""
    result = await db.conversations.delete_one({"_id": ObjectId(conversation_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    body: MessageRequest,
    background_tasks: BackgroundTasks,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    """Send a message and get the response (streaming or non-streaming)."""

    if body.stream:
        # Streaming mode - return SSE stream
        async def event_generator():
            """Generate SSE events from orchestrator stream."""
            async for chunk in orchestrator.process_message(
                conversation_id, body.content, stream=True, background_tasks=background_tasks
            ):
                yield {
                    "event": chunk.type,
                    "data": json.dumps(chunk.to_dict()),
                }

        return EventSourceResponse(event_generator())
    else:
        # Non-streaming mode - collect all chunks and return as JSON
        content_parts = []
        tool_calls = []
        usage = {}

        async for chunk in orchestrator.process_message(
            conversation_id, body.content, stream=False, background_tasks=background_tasks
        ):
            if chunk.type == "text":
                content_parts.append(chunk.content)
            elif chunk.type == "tool_call":
                tool_calls.append(chunk.to_dict()["tool_call"])
            elif chunk.type == "done":
                usage = chunk.to_dict().get("usage", {})

        return {
            "content": "".join(content_parts),
            "tool_calls": tool_calls,
            "usage": usage,
        }
