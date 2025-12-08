"""
ARIA - Agent Orchestrator

Phase: 2, 3 (Updated)
Purpose: Main agent loop - processes messages and streams responses with memory and tools

Related Spec Sections:
- Section 2.2: Request Flow
- Section 3: Memory Architecture
- Section 8: Phase 1, 2, & 3 Implementation
"""

import uuid
from datetime import datetime
from typing import AsyncIterator, Optional
from bson import ObjectId

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.llm.manager import llm_manager
from aria.llm.base import StreamChunk, Tool, Message
from aria.core.context import ContextBuilder
from aria.memory.extraction import MemoryExtractor
from aria.tools.router import ToolRouter


class Orchestrator:
    """Main agent orchestration logic with memory and tool integration."""

    def __init__(self, db: AsyncIOMotorDatabase, tool_router: Optional[ToolRouter] = None):
        self.db = db
        self.context_builder = ContextBuilder(db)
        self.memory_extractor = MemoryExtractor(db)
        self.tool_router = tool_router

    def _get_llm_with_fallback(self, agent: dict, error: Optional[Exception] = None):
        """
        Get LLM adapter with fallback support.

        Returns the primary LLM or falls back to configured fallback chain.

        Args:
            agent: Agent configuration
            error: Optional error from primary LLM attempt

        Returns:
            tuple: (adapter, llm_config, is_fallback)
        """
        # Try primary LLM first if no error
        if not error:
            llm_config = agent["llm"]
            try:
                adapter = llm_manager.get_adapter(
                    llm_config["backend"],
                    llm_config["model"]
                )
                return adapter, llm_config, False
            except Exception as e:
                # Primary failed, fall through to fallback logic
                error = e

        # Try fallback chain
        fallback_chain = agent.get("fallback_chain", [])
        for fallback_llm in fallback_chain:
            # Check if this fallback applies to the current error
            conditions = fallback_llm.get("conditions", {})

            # on_error: use fallback when any error occurs
            if error and conditions.get("on_error", True):
                try:
                    adapter = llm_manager.get_adapter(
                        fallback_llm["backend"],
                        fallback_llm["model"]
                    )
                    return adapter, fallback_llm, True
                except Exception:
                    # This fallback also failed, try next
                    continue

        # No fallback worked, raise original error
        if error:
            raise error

        # Shouldn't reach here
        raise RuntimeError("No LLM adapter available")

    async def process_message(
        self, conversation_id: str, user_message: str
    ) -> AsyncIterator[StreamChunk]:
        """
        Process a user message and stream the response.
        Handles tool calls if tools are enabled.

        Args:
            conversation_id: ID of the conversation
            user_message: User's message content

        Yields:
            StreamChunk objects for streaming response
        """
        # 1. Load conversation
        conversation = await self.db.conversations.find_one(
            {"_id": ObjectId(conversation_id)}
        )
        if not conversation:
            yield StreamChunk(type="error", error="Conversation not found")
            return

        # 2. Load agent
        agent = await self.db.agents.find_one(
            {"_id": ObjectId(conversation["agent_id"])}
        )
        if not agent:
            yield StreamChunk(type="error", error="Agent not found")
            return

        # 3. Build message list using context builder (includes memories)
        messages = await self.context_builder.build_messages(
            conversation_id=conversation_id,
            user_message=user_message,
            agent_config=agent,
            include_memories=agent.get("capabilities", {}).get("memory_enabled", True),
        )

        # 4. Save user message to conversation
        user_msg_doc = {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": user_message,
            "created_at": datetime.utcnow(),
            "memory_processed": False,
        }
        await self.db.conversations.update_one(
            {"_id": ObjectId(conversation_id)},
            {
                "$push": {"messages": user_msg_doc},
                "$set": {"updated_at": datetime.utcnow()},
                "$inc": {"stats.message_count": 1},
            },
        )

        # 5. Get LLM adapter (with fallback support)
        try:
            adapter, llm_config, is_fallback = self._get_llm_with_fallback(agent)
            if is_fallback:
                # Notify user that fallback LLM is being used
                yield StreamChunk(
                    type="text",
                    content=f"\n[Using fallback LLM: {llm_config['backend']}/{llm_config['model']}]\n\n"
                )
        except Exception as e:
            yield StreamChunk(type="error", error=f"No LLM available: {str(e)}")
            return

        # 6. Prepare tools if enabled
        tools = None
        tools_enabled = agent.get("capabilities", {}).get("tools_enabled", False)
        if tools_enabled and self.tool_router:
            enabled_tool_names = agent.get("enabled_tools", [])
            tool_definitions = self.tool_router.get_tool_definitions(enabled_tool_names)
            # Convert to LLM Tool format
            tools = [
                Tool(
                    name=td["name"],
                    description=td["description"],
                    parameters=td["parameters"],
                )
                for td in tool_definitions
            ]

        # 7. Stream response (with potential tool calling loop)
        assistant_content_parts = []
        tool_calls = []
        usage = {}

        try:
            async for chunk in adapter.stream(
                messages,
                tools=tools if tools else None,
                temperature=llm_config.get("temperature", 0.7),
                max_tokens=llm_config.get("max_tokens", 4096),
            ):
                if chunk.type == "text":
                    assistant_content_parts.append(chunk.content)
                    yield chunk
                elif chunk.type == "tool_call":
                    # Collect tool calls
                    tool_calls.append(chunk.tool_call)
                    yield chunk
                elif chunk.type == "done":
                    usage = chunk.usage
                    yield chunk
                elif chunk.type == "error":
                    yield chunk
                    return

        except Exception as e:
            yield StreamChunk(type="error", error=f"LLM error: {str(e)}")
            return

        # 8. Save assistant response
        assistant_content = "".join(assistant_content_parts)
        assistant_msg_doc = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": assistant_content,
            "model": llm_config["model"],
            "tokens": usage,
            "created_at": datetime.utcnow(),
            "memory_processed": False,
        }

        # Add tool calls if any
        if tool_calls:
            assistant_msg_doc["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                }
                for tc in tool_calls
            ]

        await self.db.conversations.update_one(
            {"_id": ObjectId(conversation_id)},
            {
                "$push": {"messages": assistant_msg_doc},
                "$set": {"updated_at": datetime.utcnow()},
                "$inc": {
                    "stats.message_count": 1,
                    "stats.total_tokens": usage.get("input_tokens", 0)
                    + usage.get("output_tokens", 0),
                    "stats.tool_calls": len(tool_calls),
                },
            },
        )

        # 9. Execute tool calls if any
        if tool_calls and self.tool_router:
            for tool_call in tool_calls:
                # Execute the tool
                result = await self.tool_router.execute_tool(
                    tool_name=tool_call.name,
                    arguments=tool_call.arguments,
                )

                # Save tool result message
                tool_result_msg = {
                    "id": str(uuid.uuid4()),
                    "role": "tool",
                    "content": str(result.output) if result.output else (result.error or ""),
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.name,
                    "status": result.status.value,
                    "created_at": datetime.utcnow(),
                    "memory_processed": False,
                }

                await self.db.conversations.update_one(
                    {"_id": ObjectId(conversation_id)},
                    {
                        "$push": {"messages": tool_result_msg},
                        "$set": {"updated_at": datetime.utcnow()},
                        "$inc": {"stats.message_count": 1},
                    },
                )

                # Yield tool result to client
                yield StreamChunk(
                    type="text",
                    content=f"\n[Tool {tool_call.name}: {result.status.value}]\n",
                )

        # 10. Queue memory extraction if enabled
        if agent.get("memory_config", {}).get("auto_extract", False):
            # Note: In production, this should be a proper background task
            # For now, we'll do it async without blocking
            try:
                import asyncio
                asyncio.create_task(
                    self.memory_extractor.extract_from_conversation(
                        conversation_id,
                        llm_backend=llm_config["backend"],
                        llm_model=llm_config["model"],
                    )
                )
            except Exception as e:
                print(f"Failed to queue memory extraction: {e}")
