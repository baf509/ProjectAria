"""
ARIA - Conversations Routes

Phase: 1
Purpose: Conversation CRUD and message handling

Related Spec Sections:
- Section 5.1: REST Endpoints
- Section 5.3: SSE Stream Format
"""

import json
import re
from datetime import datetime, timezone
from aria.api.deps import valid_object_id
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from sse_starlette.sse import EventSourceResponse

from aria.api.deps import get_db, get_orchestrator
from bson import ObjectId as BsonObjectId
from aria.db.models import (
    ConversationBranch,
    ConversationCreate,
    ConversationResponse,
    ConversationListItem,
    ConversationUpdate,
    ConversationSwitchMode,
    MessageRequest,
    Message,
    LLMConfig,
    ConversationStats,
    SteeringMessageRequest,
)
from aria.core.orchestrator import Orchestrator
from aria.core.steering import steering_queue

router = APIRouter()


def serialize_conversation(doc: dict) -> dict:
    """Convert MongoDB document to API response."""
    doc["id"] = str(doc.pop("_id"))
    doc["agent_id"] = str(doc["agent_id"])
    if doc.get("active_agent_id") is not None:
        doc["active_agent_id"] = str(doc["active_agent_id"])
    return doc


@router.get("/conversations", response_model=list[ConversationListItem])
async def list_conversations(
    limit: int = 50,
    skip: int = 0,
    status: str = "active",
    q: str | None = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List conversations."""
    query: dict = {"status": status}
    if q:
        escaped_q = re.escape(q)
        query["$or"] = [
            {"title": {"$regex": escaped_q, "$options": "i"}},
            {"summary": {"$regex": escaped_q, "$options": "i"}},
            {"messages.content": {"$regex": escaped_q, "$options": "i"}},
        ]
    cursor = (
        db.conversations.find(query)
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
        agent = await db.agents.find_one({"_id": valid_object_id(body.agent_id)})
    elif body.agent_slug:
        agent = await db.agents.find_one({"slug": body.agent_slug})
    else:
        agent = await db.agents.find_one({"is_default": True})

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Create conversation document
    now = datetime.now(timezone.utc)
    conversation = {
        "agent_id": agent["_id"],
        "active_agent_id": None,
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
        "private": body.private,
        "stats": {"message_count": 0, "total_tokens": 0, "tool_calls": 0},
    }

    # Private conversations force llamacpp backend
    if body.private:
        conversation["llm_config"]["backend"] = "llamacpp"

    result = await db.conversations.insert_one(conversation)
    conversation["_id"] = result.inserted_id

    return ConversationResponse(**serialize_conversation(conversation))


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str,
    msg_limit: int = 100,
    msg_skip: int = 0,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get a conversation with paginated messages.

    Args:
        msg_limit: Maximum number of messages to return (default 100)
        msg_skip: Number of messages to skip from the end (0 = most recent)
    """
    # Use projection to paginate messages
    if msg_skip > 0:
        # Slice from end: skip oldest, then limit
        projection = {"messages": {"$slice": [msg_skip, msg_limit]}}
    else:
        projection = {"messages": {"$slice": -msg_limit}}

    conversation = await db.conversations.find_one(
        {"_id": valid_object_id(conversation_id)},
        projection,
    )
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

    if "active_agent_id" in update_data and update_data["active_agent_id"] is not None:
        try:
            update_data["active_agent_id"] = valid_object_id(update_data["active_agent_id"])
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid active_agent_id")

    update_data["updated_at"] = datetime.now(timezone.utc)

    result = await db.conversations.update_one(
        {"_id": valid_object_id(conversation_id)}, {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Conversation not found")

    conversation = await db.conversations.find_one({"_id": valid_object_id(conversation_id)})
    return ConversationResponse(**serialize_conversation(conversation))


@router.post("/conversations/{conversation_id}/switch-mode", response_model=ConversationResponse)
async def switch_conversation_mode(
    conversation_id: str,
    body: ConversationSwitchMode,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Switch the active agent/mode for a conversation."""
    conversation = await db.conversations.find_one({"_id": valid_object_id(conversation_id)})
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    agent = await db.agents.find_one({"slug": body.agent_slug})
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    await db.conversations.update_one(
        {"_id": valid_object_id(conversation_id)},
        {
            "$set": {
                "active_agent_id": agent["_id"],
                "updated_at": datetime.now(timezone.utc),
                "llm_config": {
                    "backend": agent["llm"]["backend"],
                    "model": agent["llm"]["model"],
                    "temperature": agent["llm"]["temperature"],
                },
            }
        },
    )

    updated = await db.conversations.find_one({"_id": valid_object_id(conversation_id)})
    return ConversationResponse(**serialize_conversation(updated))


@router.post("/conversations/{conversation_id}/branch", response_model=ConversationResponse, status_code=201)
async def branch_conversation(
    conversation_id: str,
    body: ConversationBranch,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Branch a conversation at a specific message index.

    Creates a new conversation containing messages up to (and including)
    the specified index. The original conversation is unchanged.
    A summary of the branched-off portion is stored for reference.
    """
    original = await db.conversations.find_one({"_id": valid_object_id(conversation_id)})
    if not original:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = original.get("messages", [])
    if body.message_index >= len(messages):
        raise HTTPException(
            status_code=400,
            detail=f"message_index {body.message_index} exceeds message count {len(messages)}",
        )

    # Messages up to and including the branch point go into the new conversation
    branched_messages = messages[: body.message_index + 1]
    # Messages after the branch point stay in the original (generate a summary)
    remaining_messages = messages[body.message_index + 1:]

    branch_title = body.title or f"Branch of: {original.get('title', 'Untitled')}"

    now = datetime.now(timezone.utc)
    new_conversation = {
        "agent_id": original["agent_id"],
        "active_agent_id": original.get("active_agent_id"),
        "title": branch_title,
        "summary": original.get("summary"),
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "llm_config": original.get("llm_config", {}),
        "messages": branched_messages,
        "tags": [*original.get("tags", []), "branch"],
        "pinned": False,
        "stats": {
            "message_count": len(branched_messages),
            "total_tokens": 0,
            "tool_calls": 0,
        },
        "parent_conversation_id": original["_id"],
        "branch_point_index": body.message_index,
    }

    result = await db.conversations.insert_one(new_conversation)
    new_conversation["_id"] = result.inserted_id

    # Store branch metadata on the original conversation
    branch_record = {
        "conversation_id": result.inserted_id,
        "branch_point_index": body.message_index,
        "title": branch_title,
        "created_at": now,
    }
    await db.conversations.update_one(
        {"_id": valid_object_id(conversation_id)},
        {
            "$push": {"branches": branch_record},
            "$set": {"updated_at": now},
        },
    )

    return ConversationResponse(**serialize_conversation(new_conversation))


@router.get("/conversations/{conversation_id}/export")
async def export_conversation(
    conversation_id: str,
    format: str = "json",
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Export a conversation as JSON or markdown."""
    conversation = await db.conversations.find_one({"_id": valid_object_id(conversation_id)})
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    serialized = serialize_conversation(conversation)
    if format == "markdown":
        lines = [f"# {serialized['title']}", ""]
        for message in serialized.get("messages", []):
            lines.append(f"## {message['role'].upper()}")
            lines.append(message["content"])
            lines.append("")
        return {"format": "markdown", "content": "\n".join(lines)}

    return {"format": "json", "conversation": serialized}


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """Delete a conversation."""
    result = await db.conversations.delete_one({"_id": valid_object_id(conversation_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.post("/conversations/{conversation_id}/steer")
async def steer_conversation(
    conversation_id: str,
    body: SteeringMessageRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Send a mid-execution steering message to an active conversation.

    The orchestrator will pick up this message at the next safe checkpoint
    (between tool calls or LLM rounds) and incorporate it into processing.

    Priority 'interrupt' causes the current round to stop and re-prompt.
    Priority 'normal' is appended as context in the next round.
    """
    # Verify conversation exists
    exists = await db.conversations.count_documents(
        {"_id": valid_object_id(conversation_id)}, limit=1
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if body.priority not in ("normal", "interrupt"):
        raise HTTPException(status_code=400, detail="Priority must be 'normal' or 'interrupt'")

    queued = steering_queue.enqueue(conversation_id, body.content, body.priority)
    if not queued:
        raise HTTPException(status_code=429, detail="Steering queue full for this conversation")

    return {"status": "queued", "priority": body.priority}


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    body: MessageRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    """Send a message and get the response (streaming or non-streaming)."""

    # Check if OODA is enabled for this conversation's agent
    db = orchestrator.db
    conversation_doc = await db.conversations.find_one({"_id": valid_object_id(conversation_id)})
    ooda_config = None
    if conversation_doc:
        agent_id = conversation_doc.get("active_agent_id") or conversation_doc.get("agent_id")
        if agent_id:
            agent_doc = await db.agents.find_one({"_id": BsonObjectId(agent_id) if not isinstance(agent_id, BsonObjectId) else agent_id})
            if agent_doc:
                ooda_cfg = agent_doc.get("ooda", {})
                if ooda_cfg.get("enabled"):
                    ooda_config = {
                        "threshold": ooda_cfg.get("threshold", 0.7),
                        "max_retries": ooda_cfg.get("max_retries", 2),
                        "backend": agent_doc["llm"]["backend"],
                        "model": agent_doc["llm"]["model"],
                    }

    if ooda_config and not body.stream:
        # OODA mode: buffer, evaluate, retry
        content_parts = []
        usage = {}
        async for chunk in orchestrator.process_message_with_ooda(
            conversation_id, body.content, ooda_config, background_tasks=background_tasks
        ):
            if chunk.type == "text":
                content_parts.append(chunk.content)
            elif chunk.type == "done":
                usage = chunk.usage or {}
        return {"content": "".join(content_parts), "usage": usage}

    if body.stream:
        # Streaming mode - return SSE stream with heartbeat keep-alive
        async def event_generator():
            """Generate SSE events from orchestrator stream with heartbeat."""
            import asyncio

            last_event_id_header = request.headers.get("last-event-id")
            try:
                event_id = int(last_event_id_header) + 1 if last_event_id_header else 1
            except ValueError:
                event_id = 1

            heartbeat_interval = 15  # seconds

            stream_iter = orchestrator.process_message(
                conversation_id, body.content, stream=True, background_tasks=background_tasks
            ).__aiter__()

            while True:
                try:
                    chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=heartbeat_interval)
                    yield {
                        "id": str(event_id),
                        "event": chunk.type,
                        "data": json.dumps(chunk.to_dict()),
                    }
                    event_id += 1
                except asyncio.TimeoutError:
                    # Send SSE comment as heartbeat to keep connection alive
                    yield {"comment": "heartbeat"}
                except StopAsyncIteration:
                    break
                except Exception as exc:
                    yield {
                        "id": str(event_id),
                        "event": "error",
                        "data": json.dumps({"type": "error", "error": str(exc)}),
                    }
                    break

        return EventSourceResponse(
            event_generator(),
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
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
