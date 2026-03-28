"""
ARIA - Command Router

Phase: 3
Purpose: Extract command parsing and dispatch from the orchestrator into a clean middleware

Related Spec Sections:
- Section 2.2: Request Flow
"""

import logging
import re as re_module
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.agents.session import CodingSessionManager
from aria.config import settings
from aria.memory.extraction import MemoryExtractor
from aria.memory.long_term import LongTermMemory
from aria.research.service import ResearchService


@dataclass
class CommandResult:
    """Result from a successfully handled command."""

    assistant_content: str
    persist_message: bool = True
    continues_to_llm: bool = False
    error: Optional[str] = None
    extra: dict = field(default_factory=dict)


class CommandRouter:
    """Parses user messages for commands and dispatches to appropriate handlers.

    Commands handled:
    - Mode switching: /mode, "switch to X mode", "use X mode"
    - Models listing: /models, /backends, "list models"
    - Backend switching: "use local", "use openrouter", "switch to cloud", etc.
    - Research dispatch: /research, "research ..."
    - Memory commands: "remember that", "forget about", "what do you know about", "show my memories"
    - Coding commands: /code, /coding-status, /coding-stop
    - Auto-mode detection from agent keywords
    - Auto-start coding sessions for coding-oriented agents
    """

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        memory_extractor: MemoryExtractor,
        long_term_memory: LongTermMemory,
        research_service: Optional[ResearchService] = None,
        coding_manager: Optional[CodingSessionManager] = None,
    ):
        self.db = db
        self.memory_extractor = memory_extractor
        self.long_term_memory = long_term_memory
        self.research_service = research_service
        self.coding_manager = coding_manager

    async def try_handle(
        self,
        conversation_id: str,
        user_message: str,
    ) -> Optional[CommandResult]:
        """Try to handle the message as a command.

        Returns a CommandResult if the message was a command, None otherwise.
        The caller (orchestrator) is responsible for persisting and streaming.
        """
        # Try each command type in order of priority
        result = await self._handle_mode_command(conversation_id, user_message)
        if result is not None:
            return result

        result = await self._handle_models_command(user_message)
        if result is not None:
            return result

        result = await self._handle_backend_switch(conversation_id, user_message)
        if result is not None:
            return result

        result = await self._handle_research_command(conversation_id, user_message)
        if result is not None:
            return result

        result = await self._handle_memory_command(conversation_id, user_message)
        if result is not None:
            return result

        result = await self._handle_coding_command(conversation_id, user_message)
        if result is not None:
            return result

        return None

    async def try_handle_contextual(
        self,
        conversation_id: str,
        user_message: str,
        agent: Optional[dict],
    ) -> Optional[CommandResult]:
        """Handle commands that require the resolved agent context.

        These are checked after the agent is loaded but before LLM streaming:
        - Auto-mode detection from keywords
        - Auto-start coding sessions

        Returns a CommandResult if handled, None otherwise.
        """
        detected_mode = await self._maybe_detect_mode_from_message(
            conversation_id, user_message, agent
        )
        if detected_mode is not None:
            return detected_mode

        if agent is not None:
            auto_coding = await self._maybe_autostart_coding_session(
                conversation_id, user_message, agent
            )
            if auto_coding is not None:
                return auto_coding

        return None

    # ------------------------------------------------------------------
    # Mode commands
    # ------------------------------------------------------------------

    async def _handle_mode_command(
        self,
        conversation_id: str,
        user_message: str,
    ) -> Optional[CommandResult]:
        """Handle explicit /mode command and return a result payload."""
        stripped = user_message.strip()
        lowered = stripped.lower()
        mode_slug = None

        if lowered.startswith("/mode "):
            mode_slug = stripped[6:].strip()
        elif lowered.startswith("switch to ") and lowered.endswith(" mode"):
            mode_slug = stripped[10:-5].strip()
        elif lowered.startswith("use ") and lowered.endswith(" mode"):
            mode_slug = stripped[4:-5].strip()

        if mode_slug is None:
            return None
        if not mode_slug:
            return CommandResult(assistant_content="", error="Usage: /mode <agent-slug>")

        agent = await self.db.agents.find_one(
            {
                "$or": [
                    {"slug": mode_slug},
                    {"slug": mode_slug.lower().replace(" ", "-")},
                    {"name": {"$regex": f"^{re_module.escape(mode_slug)}$", "$options": "i"}},
                ]
            }
        )
        if not agent:
            return CommandResult(assistant_content="", error=f"Mode '{mode_slug}' not found")

        await self.db.conversations.update_one(
            {"_id": ObjectId(conversation_id)},
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

        assistant_content = f"Switched to mode '{agent['slug']}' ({agent['name']})."
        assistant_msg_doc = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": assistant_content,
            "model": agent["llm"]["model"],
            "created_at": datetime.now(timezone.utc),
            "memory_processed": False,
        }
        await self.db.conversations.update_one(
            {"_id": ObjectId(conversation_id)},
            {
                "$push": {"messages": assistant_msg_doc},
                "$set": {"updated_at": datetime.now(timezone.utc)},
                "$inc": {"stats.message_count": 1},
            },
        )
        return CommandResult(assistant_content=assistant_content, persist_message=False)

    # ------------------------------------------------------------------
    # Models / backend listing
    # ------------------------------------------------------------------

    # Default models per backend — used when switching via natural language
    _BACKEND_DEFAULTS: dict[str, str] = {
        "llamacpp": "default",
        "openrouter": "minimax/minimax-m2.7",
        "anthropic": "claude-sonnet-4-20250514",
        "openai": "gpt-4o",
    }

    async def _handle_models_command(
        self,
        user_message: str,
    ) -> Optional[CommandResult]:
        """Handle /models command — list available backends and current selection."""
        lowered = user_message.strip().lower()
        if lowered not in ("/models", "/backends", "list models", "what models are available"):
            return None

        backends = []
        backends.append(f"  local (llamacpp) — {self._BACKEND_DEFAULTS['llamacpp']}")
        if settings.openrouter_api_key:
            backends.append(f"  openrouter — {self._BACKEND_DEFAULTS['openrouter']}")
        if settings.anthropic_api_key:
            backends.append(f"  anthropic — {self._BACKEND_DEFAULTS['anthropic']}")
        if settings.openai_api_key:
            backends.append(f"  openai — {self._BACKEND_DEFAULTS['openai']}")

        lines = ["Available backends:"]
        lines.extend(backends)
        lines.append('\nSwitch with: "use local", "use openrouter", "use claude", etc.')
        lines.append('Reset to default: "use default"')

        return CommandResult(assistant_content="\n".join(lines))

    # ------------------------------------------------------------------
    # Natural-language backend switching
    # ------------------------------------------------------------------

    # Maps natural-language aliases → backend names
    _BACKEND_ALIASES: dict[str, str] = {
        "local": "llamacpp",
        "llama": "llamacpp",
        "llamacpp": "llamacpp",
        "openrouter": "openrouter",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "openai": "openai",
        "gpt": "openai",
        "cloud": "openrouter",
    }

    _BACKEND_SWITCH_PATTERN = re_module.compile(
        r"^(?:use|switch to|change to|go to|talk to)\s+(.+?)(?:\s+model)?$",
        re_module.IGNORECASE,
    )

    async def _handle_backend_switch(
        self,
        conversation_id: str,
        user_message: str,
    ) -> Optional[CommandResult]:
        """Handle natural-language backend switching like 'use local' or 'switch to openrouter'.

        Sets llm_config_override on the conversation so the orchestrator uses
        that backend/model instead of the agent's default. The agent stays the same.
        """
        match = self._BACKEND_SWITCH_PATTERN.match(user_message.strip())
        if not match:
            return None

        requested = match.group(1).strip().lower()

        # "use default" clears the override
        if requested == "default":
            await self.db.conversations.update_one(
                {"_id": ObjectId(conversation_id)},
                {
                    "$unset": {"llm_config_override": ""},
                    "$set": {"updated_at": datetime.now(timezone.utc)},
                },
            )
            assistant_content = "Switched back to the default model."
            assistant_msg_doc = {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": assistant_content,
                "created_at": datetime.now(timezone.utc),
                "memory_processed": False,
            }
            await self.db.conversations.update_one(
                {"_id": ObjectId(conversation_id)},
                {
                    "$push": {"messages": assistant_msg_doc},
                    "$set": {"updated_at": datetime.now(timezone.utc)},
                    "$inc": {"stats.message_count": 1},
                },
            )
            return CommandResult(assistant_content=assistant_content, persist_message=False)

        backend = self._BACKEND_ALIASES.get(requested)
        if backend is None:
            return None

        model = self._BACKEND_DEFAULTS.get(backend, "default")

        # Check if already using this backend
        conversation = await self.db.conversations.find_one({"_id": ObjectId(conversation_id)})
        current_override = (conversation or {}).get("llm_config_override", {})
        if current_override.get("backend") == backend:
            return CommandResult(
                assistant_content=f"Already using {backend}/{model}.",
            )

        override = {
            "backend": backend,
            "model": model,
            "temperature": 0.7,
        }
        await self.db.conversations.update_one(
            {"_id": ObjectId(conversation_id)},
            {
                "$set": {
                    "llm_config_override": override,
                    "updated_at": datetime.now(timezone.utc),
                },
            },
        )

        assistant_content = f"Switched to {backend}/{model}."
        assistant_msg_doc = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": assistant_content,
            "model": model,
            "created_at": datetime.now(timezone.utc),
            "memory_processed": False,
        }
        await self.db.conversations.update_one(
            {"_id": ObjectId(conversation_id)},
            {
                "$push": {"messages": assistant_msg_doc},
                "$set": {"updated_at": datetime.now(timezone.utc)},
                "$inc": {"stats.message_count": 1},
            },
        )
        return CommandResult(assistant_content=assistant_content, persist_message=False)

    # ------------------------------------------------------------------
    # Research commands
    # ------------------------------------------------------------------

    async def _handle_research_command(
        self,
        conversation_id: str,
        user_message: str,
    ) -> Optional[CommandResult]:
        """Handle simple research commands and return a result payload."""
        if self.research_service is None:
            return None

        stripped = user_message.strip()
        lowered = stripped.lower()

        query = None
        if lowered.startswith("/research "):
            query = stripped[10:].strip().strip("\"'")
        elif lowered.startswith("research "):
            query = stripped[9:].strip().strip("\"'")

        if not query:
            return None

        result = await self.research_service.start_research(
            query=query,
            conversation_id=conversation_id,
        )
        assistant_content = (
            f"Starting deep research on '{query}'. "
            f"I'll notify you when it's done. Research ID: {result['research_id']}."
        )
        assistant_msg_doc = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": assistant_content,
            "created_at": datetime.now(timezone.utc),
            "memory_processed": False,
        }
        await self.db.conversations.update_one(
            {"_id": ObjectId(conversation_id)},
            {
                "$push": {"messages": assistant_msg_doc},
                "$set": {"updated_at": datetime.now(timezone.utc)},
                "$inc": {"stats.message_count": 1},
            },
        )
        return CommandResult(assistant_content=assistant_content, persist_message=False)

    # ------------------------------------------------------------------
    # Memory commands
    # ------------------------------------------------------------------

    async def _handle_memory_command(
        self,
        conversation_id: str,
        user_message: str,
    ) -> Optional[CommandResult]:
        """Handle conversational memory commands inline."""
        stripped = user_message.strip()
        lowered = stripped.lower()

        if lowered.startswith("remember that "):
            memory_text = stripped[len("remember that "):].strip()
            extracted = await self.memory_extractor.extract_from_text(memory_text)
            created = 0
            for item in extracted or [{
                "content": memory_text,
                "content_type": "fact",
                "categories": ["conversation"],
                "importance": 0.7,
            }]:
                await self.long_term_memory.create_memory(
                    content=item["content"],
                    content_type=item.get("content_type", "fact"),
                    categories=item.get("categories", []),
                    importance=item.get("importance", 0.7),
                    confidence=0.95,
                    source={"type": "conversation_command", "conversation_id": ObjectId(conversation_id)},
                )
                created += 1
            summary = extracted[0]["content"] if extracted else memory_text
            return CommandResult(
                assistant_content=f"Got it, I'll remember that {summary}",
                extra={"created": created},
            )

        if lowered.startswith("forget about "):
            query = stripped[len("forget about "):].strip()
            matches = await self.long_term_memory.search(query, limit=10)
            removed = 0
            for memory in matches:
                await self.long_term_memory.delete_memory(memory.id)
                removed += 1
            return CommandResult(
                assistant_content=f"I found {removed} matching memories and removed them.",
            )

        if lowered.startswith("what do you know about "):
            query = stripped[len("what do you know about "):].strip()
            matches = await self.long_term_memory.search(query, limit=5)
            if not matches:
                return CommandResult(
                    assistant_content=f"I don't have any relevant memories about '{query}' yet.",
                )
            bullet_lines = [f"- {memory.content}" for memory in matches]
            return CommandResult(
                assistant_content="Here's what I remember:\n" + "\n".join(bullet_lines),
            )

        if lowered in {"what do you remember?", "what do you remember", "show my memories"}:
            docs = await self.db.memories.find({"status": "active"}).sort("created_at", -1).limit(10).to_list(length=10)
            if not docs:
                return CommandResult(assistant_content="I don't have any saved memories yet.")
            bullet_lines = [f"- {doc['content']}" for doc in docs]
            return CommandResult(
                assistant_content="Recent memories:\n" + "\n".join(bullet_lines),
            )

        return None

    # ------------------------------------------------------------------
    # Coding commands
    # ------------------------------------------------------------------

    async def _handle_coding_command(
        self,
        conversation_id: str,
        user_message: str,
    ) -> Optional[CommandResult]:
        """Handle explicit coding-session commands inline."""
        if self.coding_manager is None:
            return None

        stripped = user_message.strip()
        lowered = stripped.lower()

        if lowered.startswith("/code "):
            prompt = stripped[6:].strip()
            session = await self.coding_manager.start_session(
                workspace=settings.coding_default_workspace,
                backend=None,
                prompt=prompt,
                conversation_id=conversation_id,
            )
            return CommandResult(
                assistant_content=f"Started coding session {session['_id']} in {session['workspace']}.",
            )

        if lowered in {"how's the coding going?", "hows the coding going?", "/coding-status"}:
            sessions = await self.coding_manager.list_sessions(status="running")
            if not sessions:
                return CommandResult(assistant_content="There are no running coding sessions.")
            session = sessions[0]
            output = self.coding_manager.get_output(session["_id"], lines=12)
            return CommandResult(
                assistant_content=(
                    f"Active coding session {session['_id']} ({session['backend']})\n"
                    f"Recent output:\n{output or '<no output yet>'}"
                ),
            )

        if lowered.startswith("/coding-stop"):
            parts = stripped.split(maxsplit=1)
            session_id = None
            if len(parts) > 1:
                session_id = parts[1].strip()
            else:
                sessions = await self.coding_manager.list_sessions(status="running")
                if sessions:
                    session_id = sessions[0]["_id"]
            if not session_id:
                return CommandResult(assistant_content="No running coding session found to stop.")
            stopped = await self.coding_manager.stop_session(session_id)
            return CommandResult(
                assistant_content=f"Stopped coding session {session_id}." if stopped else f"Could not stop coding session {session_id}.",
            )

        return None

    # ------------------------------------------------------------------
    # Auto-detection (contextual — requires agent)
    # ------------------------------------------------------------------

    async def _maybe_detect_mode_from_message(
        self,
        conversation_id: str,
        user_message: str,
        current_agent: Optional[dict],
    ) -> Optional[CommandResult]:
        """Switch modes when an agent advertises matching keywords."""
        message = user_message.lower()
        if current_agent and current_agent.get("mode_metadata", {}).get("keywords"):
            current_keywords = [kw.lower() for kw in current_agent["mode_metadata"]["keywords"]]
            if any(keyword in message for keyword in current_keywords):
                return None

        agents = await self.db.agents.find({"mode_metadata.keywords": {"$exists": True, "$ne": []}}).to_list(length=100)
        for agent in agents:
            keywords = [kw.lower() for kw in agent.get("mode_metadata", {}).get("keywords", [])]
            if not keywords:
                continue
            if any(keyword in message for keyword in keywords):
                await self.db.conversations.update_one(
                    {"_id": ObjectId(conversation_id)},
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
                return CommandResult(
                    assistant_content=f"Switching to {agent['name']} mode for this request.",
                    continues_to_llm=True,
                )
        return None

    async def _maybe_autostart_coding_session(
        self,
        conversation_id: str,
        user_message: str,
        agent: dict,
    ) -> Optional[CommandResult]:
        """Autonomously start coding sessions when in a coding-oriented mode."""
        if self.coding_manager is None:
            return None
        mode_category = agent.get("mode_category", "")
        slug = str(agent.get("slug", "")).lower()
        if mode_category != "coding" and "coding" not in slug and "program" not in slug:
            return None

        lowered = user_message.lower()
        coding_verbs = [
            "implement",
            "fix",
            "debug",
            "refactor",
            "add a feature",
            "write code",
            "update the code",
            "change the code",
        ]
        if not any(phrase in lowered for phrase in coding_verbs):
            return None

        session = await self.coding_manager.start_session(
            workspace=settings.coding_default_workspace,
            backend=None,
            prompt=user_message,
            conversation_id=conversation_id,
        )
        return CommandResult(
            assistant_content=(
                f"Started coding session {session['_id']} in {session['workspace']} "
                f"using {session['backend']}. I'll monitor it and you can ask for status anytime."
            ),
        )
