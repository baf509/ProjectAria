"""
ARIA - Group Chat Service

Purpose: Orchestrate multi-persona debate sessions where agents
respond to a shared question in rounds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Optional
from uuid import uuid4

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.llm.base import StreamChunk, Message
from aria.llm.manager import llm_manager

logger = logging.getLogger(__name__)


class GroupChatService:
    """Multi-persona debate service."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db

    async def create_session(
        self,
        question: str,
        persona_ids: list[str],
        rounds: int = 0,
        synthesis: bool = True,
    ) -> dict:
        """Create a new group chat/debate session."""
        if rounds <= 0:
            rounds = settings.groupchat_default_rounds

        if len(persona_ids) > settings.groupchat_max_personas:
            raise ValueError(
                f"Too many personas ({len(persona_ids)}). "
                f"Max: {settings.groupchat_max_personas}"
            )

        # Validate all persona IDs exist as agents
        personas = []
        for pid in persona_ids:
            agent = await self.db.agents.find_one({"_id": ObjectId(pid)})
            if not agent:
                raise ValueError(f"Agent not found: {pid}")
            personas.append({
                "agent_id": str(agent["_id"]),
                "name": agent["name"],
                "backend": agent["llm"]["backend"],
                "model": agent["llm"]["model"],
                "system_prompt": agent.get("system_prompt", ""),
            })

        now = datetime.now(timezone.utc)
        session = {
            "_id": str(uuid4()),
            "question": question,
            "personas": personas,
            "rounds": rounds,
            "synthesis": synthesis,
            "transcript": [],
            "status": "created",
            "created_at": now,
            "updated_at": now,
        }
        await self.db.groupchat_sessions.insert_one(session)
        return {
            "session_id": session["_id"],
            "question": question,
            "personas": [{"agent_id": p["agent_id"], "name": p["name"]} for p in personas],
            "rounds": rounds,
        }

    async def run_debate(self, session_id: str) -> AsyncIterator[StreamChunk]:
        """Run the debate, streaming each persona's contribution."""
        session = await self.db.groupchat_sessions.find_one({"_id": session_id})
        if not session:
            yield StreamChunk(type="error", error="Session not found")
            return

        await self.db.groupchat_sessions.update_one(
            {"_id": session_id},
            {"$set": {"status": "running", "updated_at": datetime.now(timezone.utc)}},
        )

        question = session["question"]
        personas = session["personas"]
        rounds = session["rounds"]
        transcript: list[dict] = []

        for round_num in range(1, rounds + 1):
            yield StreamChunk(type="text", content=f"\n--- Round {round_num} ---\n")

            for persona in personas:
                yield StreamChunk(
                    type="text",
                    content=f"\n**{persona['name']}**: ",
                )

                # Build messages with full transcript context
                system = persona.get("system_prompt", "")
                if system:
                    system += "\n\n"
                system += (
                    "You are participating in a group discussion. "
                    "Respond to the question and consider other participants' views. "
                    "Be concise and focused."
                )

                user_content = f"Question: {question}\n\n"
                if transcript:
                    user_content += "Discussion so far:\n"
                    for entry in transcript:
                        user_content += f"- {entry['persona']}: {entry['content']}\n"
                    user_content += f"\nYour turn, {persona['name']}. Respond:"

                messages = [
                    Message(role="system", content=system),
                    Message(role="user", content=user_content),
                ]

                try:
                    adapter = llm_manager.get_adapter(persona["backend"], persona["model"])
                    response_parts = []

                    async for chunk in adapter.stream(
                        messages, temperature=0.7, max_tokens=1024, stream=True
                    ):
                        if chunk.type == "text":
                            response_parts.append(chunk.content)
                            yield StreamChunk(type="text", content=chunk.content)

                    response_text = "".join(response_parts).strip()
                    transcript.append({
                        "round": round_num,
                        "persona": persona["name"],
                        "agent_id": persona["agent_id"],
                        "content": response_text,
                    })

                except Exception as exc:
                    error_msg = f"[Error from {persona['name']}: {exc}]"
                    transcript.append({
                        "round": round_num,
                        "persona": persona["name"],
                        "agent_id": persona["agent_id"],
                        "content": error_msg,
                    })
                    yield StreamChunk(type="text", content=error_msg)

                yield StreamChunk(type="text", content="\n")

        # Optional synthesis round
        if session.get("synthesis") and transcript:
            yield StreamChunk(type="text", content="\n--- Synthesis ---\n")

            synthesis_prompt = f"Question: {question}\n\nDebate transcript:\n"
            for entry in transcript:
                synthesis_prompt += f"- {entry['persona']}: {entry['content']}\n"
            synthesis_prompt += (
                "\nSynthesize the key points of agreement and disagreement. "
                "Provide a balanced summary."
            )

            # Use the first persona's backend for synthesis
            first = personas[0]
            messages = [Message(role="user", content=synthesis_prompt)]
            try:
                adapter = llm_manager.get_adapter(first["backend"], first["model"])
                async for chunk in adapter.stream(
                    messages, temperature=0.5, max_tokens=1024, stream=True
                ):
                    if chunk.type == "text":
                        yield StreamChunk(type="text", content=chunk.content)
            except Exception as exc:
                yield StreamChunk(type="text", content=f"[Synthesis error: {exc}]")

            yield StreamChunk(type="text", content="\n")

        # Persist transcript
        await self.db.groupchat_sessions.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "transcript": transcript,
                    "status": "completed",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

        yield StreamChunk(type="done", usage={})

    async def get_transcript(self, session_id: str) -> Optional[dict]:
        """Get the transcript for a completed session."""
        session = await self.db.groupchat_sessions.find_one({"_id": session_id})
        if not session:
            return None
        return {
            "session_id": session["_id"],
            "question": session["question"],
            "status": session["status"],
            "transcript": session.get("transcript", []),
            "created_at": session.get("created_at"),
        }
