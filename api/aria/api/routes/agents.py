"""
ARIA - Agents Routes

Phase: 1
Purpose: Agent CRUD operations

Related Spec Sections:
- Section 5.1: REST Endpoints
"""

from datetime import datetime
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.api.deps import get_db
from aria.db.models import AgentCreate, AgentResponse, AgentUpdate

router = APIRouter()


def serialize_agent(doc: dict) -> dict:
    """Convert MongoDB document to API response."""
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.get("/agents", response_model=list[AgentResponse])
async def list_agents(db: AsyncIOMotorDatabase = Depends(get_db)):
    """List all agents."""
    cursor = db.agents.find().sort("created_at", -1)
    agents = []
    async for doc in cursor:
        agents.append(AgentResponse(**serialize_agent(doc)))
    return agents


@router.post("/agents", response_model=AgentResponse, status_code=201)
async def create_agent(
    body: AgentCreate, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """Create a new agent."""
    # Check if slug already exists
    existing = await db.agents.find_one({"slug": body.slug})
    if existing:
        raise HTTPException(status_code=409, detail="Agent slug already exists")

    # Create agent document
    now = datetime.utcnow()
    agent = body.model_dump()
    agent.update(
        {
            "is_default": False,
            "created_at": now,
            "updated_at": now,
        }
    )

    result = await db.agents.insert_one(agent)
    agent["_id"] = result.inserted_id

    return AgentResponse(**serialize_agent(agent))


@router.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Get an agent by ID."""
    agent = await db.agents.find_one({"_id": ObjectId(agent_id)})
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return AgentResponse(**serialize_agent(agent))


@router.put("/agents/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: str, body: AgentUpdate, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """Update an agent."""
    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    update_data["updated_at"] = datetime.utcnow()

    result = await db.agents.update_one(
        {"_id": ObjectId(agent_id)}, {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent = await db.agents.find_one({"_id": ObjectId(agent_id)})
    return AgentResponse(**serialize_agent(agent))


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Delete an agent."""
    # Cannot delete default agent
    agent = await db.agents.find_one({"_id": ObjectId(agent_id)})
    if agent and agent.get("is_default"):
        raise HTTPException(status_code=400, detail="Cannot delete default agent")

    result = await db.agents.delete_one({"_id": ObjectId(agent_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Agent not found")
