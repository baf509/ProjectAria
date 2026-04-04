"""
ARIA - Agent Orchestrator

Phase: 2, 3 (Updated)
Purpose: Main agent loop - processes messages and streams responses with memory and tools

Related Spec Sections:
- Section 2.2: Request Flow
- Section 3: Memory Architecture
- Section 8: Phase 1, 2, & 3 Implementation
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Optional
from bson import ObjectId

logger = logging.getLogger(__name__)

from motor.motor_asyncio import AsyncIOMotorDatabase
from fastapi import BackgroundTasks

from aria.agents.session import CodingSessionManager
from aria.config import settings
from aria.core.commands import CommandRouter
from aria.llm.manager import llm_manager
from aria.llm.base import StreamChunk, Tool, Message
from aria.core.context import ContextBuilder
from aria.core.summarization import maybe_update_conversation_summary
from aria.memory.extraction import MemoryExtractor
from aria.memory.long_term import LongTermMemory
from aria.db.usage import UsageRepo
from aria.research.service import ResearchService
from aria.tasks.runner import TaskRunner
from aria.tools.router import ToolRouter
from aria.core.ooda import OODALoop
from aria.core.steering import steering_queue
from aria.core.hooks import hook_registry


class Orchestrator:
    """Main agent orchestration logic with memory and tool integration."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        tool_router: Optional[ToolRouter] = None,
        task_runner: Optional[TaskRunner] = None,
        coding_manager: Optional[CodingSessionManager] = None,
    ):
        self.db = db
        self.context_builder = ContextBuilder(db)
        self.memory_extractor = MemoryExtractor(db)
        self.long_term_memory = LongTermMemory(db)
        self.usage_repo = UsageRepo(db)
        self.tool_router = tool_router
        self.research_service = ResearchService(db, task_runner) if task_runner else None
        self.coding_manager = coding_manager

        # Command router handles all command parsing and dispatch
        self.command_router = CommandRouter(
            db=db,
            memory_extractor=self.memory_extractor,
            long_term_memory=self.long_term_memory,
            research_service=self.research_service,
            coding_manager=self.coding_manager,
        )

    def _get_llm_candidates(self, agent: dict, conversation: dict | None = None) -> list[tuple]:
        """Build LLM candidate list from conversation override, primary, plus fallbacks."""
        # Private conversations are forced to llamacpp — no fallbacks
        if (conversation or {}).get("private"):
            local_config = {**agent["llm"], "backend": "llamacpp"}
            return [(local_config, False)]

        # Conversation-level override takes priority (e.g. user said "use openrouter")
        override = (conversation or {}).get("llm_config_override")
        if override and override.get("backend"):
            candidates = [(override, False)]
        else:
            candidates = [(agent["llm"], False)]
        fallback_chain = agent.get("fallback_chain", [])
        for fallback_llm in fallback_chain:
            conditions = fallback_llm.get("conditions", {})
            if conditions.get("on_error", True):
                candidates.append((fallback_llm, True))
        return candidates

    async def _resolve_active_agent(self, conversation: dict) -> Optional[dict]:
        """Resolve the effective agent for a conversation."""
        agent_id = conversation.get("active_agent_id") or conversation.get("agent_id")
        if not agent_id:
            return None
        return await self.db.agents.find_one({"_id": ObjectId(agent_id)})

    async def _persist_assistant_message(
        self,
        conversation_id: str,
        content: str,
        model: Optional[str] = None,
    ) -> None:
        """Save an assistant message to the conversation."""
        assistant_msg_doc = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": content,
            "created_at": datetime.now(timezone.utc),
            "memory_processed": False,
        }
        if model:
            assistant_msg_doc["model"] = model
        await self.db.conversations.update_one(
            {"_id": ObjectId(conversation_id)},
            {
                "$push": {"messages": assistant_msg_doc},
                "$set": {"updated_at": datetime.now(timezone.utc)},
                "$inc": {"stats.message_count": 1},
            },
        )

    async def process_message(
        self, conversation_id: str, user_message: str, stream: bool = True, background_tasks: Optional[BackgroundTasks] = None
    ) -> AsyncIterator[StreamChunk]:
        """
        Process a user message and stream the response.
        Handles tool calls if tools are enabled.

        Args:
            conversation_id: ID of the conversation
            user_message: User’s message content
            stream: Whether to use streaming mode (can be overridden by agent config)

        Yields:
            StreamChunk objects for streaming response
        """
        # 0. Fire pre_message hook
        hook_ctx = await hook_registry.fire("pre_message", {
            "conversation_id": conversation_id,
            "user_message": user_message,
            "stream": stream,
        })
        user_message = hook_ctx.get("user_message", user_message)

        # 1. Load conversation
        conversation = await self.db.conversations.find_one(
            {"_id": ObjectId(conversation_id)}
        )
        if not conversation:
            yield StreamChunk(type="error", error="Conversation not found")
            return

        # 2. Save user message to conversation
        user_msg_doc = {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": user_message,
            "created_at": datetime.now(timezone.utc),
            "memory_processed": False,
        }
        await self.db.conversations.update_one(
            {"_id": ObjectId(conversation_id)},
            {
                "$push": {"messages": user_msg_doc},
                "$set": {"updated_at": datetime.now(timezone.utc)},
                "$inc": {"stats.message_count": 1},
            },
        )

        # 3. Try command handling (mode, research, memory, coding)
        command_result = await self.command_router.try_handle(conversation_id, user_message)
        if command_result is not None:
            if command_result.error:
                yield StreamChunk(type="error", error=command_result.error)
            else:
                if command_result.persist_message:
                    await self._persist_assistant_message(
                        conversation_id, command_result.assistant_content
                    )
                yield StreamChunk(type="text", content=command_result.assistant_content)
                yield StreamChunk(type="done", usage={})
            return

        # 4. Load effective agent (reuse conversation from step 1 — no command modified it)
        agent = await self._resolve_active_agent(conversation)
        if not agent:
            yield StreamChunk(type="error", error="Agent not found")
            return

        # 5. Try contextual commands (auto-mode detection, auto-coding)
        contextual_result = await self.command_router.try_handle_contextual(
            conversation_id, user_message, agent
        )
        if contextual_result is not None:
            await self._persist_assistant_message(
                conversation_id, contextual_result.assistant_content
            )
            if contextual_result.continues_to_llm:
                # Auto-mode detection: emit text and continue to LLM streaming
                yield StreamChunk(type="text", content=contextual_result.assistant_content + "\n\n")
                conversation = await self.db.conversations.find_one({"_id": ObjectId(conversation_id)})
                agent = await self._resolve_active_agent(conversation)
                if not agent:
                    yield StreamChunk(type="error", error="Agent not found")
                    return
            else:
                # Auto-coding: return immediately
                yield StreamChunk(type="text", content=contextual_result.assistant_content)
                yield StreamChunk(type="done", usage={})
                return

        # 6. Build message list using context builder (includes memories)
        is_private = conversation.get("private", False)
        messages = await self.context_builder.build_messages(
            conversation_id=conversation_id,
            user_message=user_message,
            agent_config=agent,
            include_memories=agent.get("capabilities", {}).get("memory_enabled", True),
            private=is_private,
        )

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

        # 7. Fire pre_llm_call hook
        await hook_registry.fire("pre_llm_call", {
            "conversation_id": conversation_id,
            "messages": messages,
            "tools": tools,
            "agent": agent,
        })

        # 8. Stream response with tool-call loop
        # The LLM may request tool calls; we execute them and feed results
        # back to the LLM for a follow-up response, up to max_tool_rounds.
        max_tool_rounds = 10
        all_assistant_content_parts = []
        total_usage = {}
        llm_config = None
        candidate_error = None
        candidate_used_fallback = False
        candidate_backend_name = None

        for _tool_round in range(max_tool_rounds + 1):
            assistant_content_parts = []
            tool_calls = []
            usage = {}
            # Track <think>...</think> blocks so we can strip them from output
            # but capture content for cross-provider fallback context
            in_think_block = False
            think_buffer = ""
            think_content_parts = []  # Captured thinking for fallback context

            round_llm_config = None

            for candidate_llm_config, is_fallback in self._get_llm_candidates(agent, conversation):
                emitted_output = False
                backend_name = candidate_llm_config["backend"]

                # Check circuit breaker before attempting this backend
                if not await llm_manager.is_backend_healthy(backend_name):
                    logger.warning("Skipping backend %s: circuit breaker open", backend_name)
                    continue

                try:
                    adapter = llm_manager.get_adapter(
                        backend_name,
                        candidate_llm_config["model"],
                    )
                    if is_fallback:
                        primary_backend = self._get_llm_candidates(agent, conversation)[0][0]["backend"]
                        llm_manager.record_fallback(primary_backend, backend_name)
                        await hook_registry.fire("on_fallback", {
                            "conversation_id": conversation_id,
                            "primary_backend": primary_backend,
                            "fallback_backend": backend_name,
                            "model": candidate_llm_config["model"],
                        })
                        yield StreamChunk(
                            type="text",
                            content=f"\n[Using fallback LLM: {backend_name}/{candidate_llm_config['model']}]\n\n",
                        )
                        # Cross-provider thinking block conversion:
                        # If the previous provider emitted thinking blocks before failing,
                        # inject them as context for the fallback provider
                        if think_content_parts:
                            captured_thinking = "".join(think_content_parts)
                            if captured_thinking.strip():
                                thinking_context = Message(
                                    role="assistant",
                                    content=f"[Previous reasoning from {primary_backend} before fallback]\n{captured_thinking.strip()}",
                                )
                                messages.append(thinking_context)
                                messages.append(Message(
                                    role="user",
                                    content=user_message + "\n\n[Note: The previous LLM provider failed mid-response. The reasoning above was captured. Please continue from where it left off.]",
                                ))
                            think_content_parts.clear()

                    use_streaming = stream and not candidate_llm_config.get("force_non_streaming", False)
                    chunk_timeout = settings.stream_chunk_timeout_seconds
                    stream_iter = adapter.stream(
                        messages,
                        tools=tools if tools else None,
                        temperature=candidate_llm_config.get("temperature", 0.7),
                        max_tokens=candidate_llm_config.get("max_tokens", 4096),
                        stream=use_streaming,
                    ).__aiter__()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=chunk_timeout)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise RuntimeError(f"LLM stream stalled: no chunk received in {chunk_timeout}s")
                        if chunk.type == "text":
                            emitted_output = True
                            text = think_buffer + chunk.content
                            think_buffer = ""
                            filtered = ""

                            while text:
                                if in_think_block:
                                    end_idx = text.find("</think>")
                                    if end_idx != -1:
                                        # Capture thinking content for cross-provider fallback
                                        think_content_parts.append(text[:end_idx])
                                        in_think_block = False
                                        text = text[end_idx + 8:]
                                    else:
                                        # Still inside think block — capture and consume
                                        think_content_parts.append(text)
                                        break
                                else:
                                    start_idx = text.find("<think>")
                                    if start_idx != -1:
                                        filtered += text[:start_idx]
                                        in_think_block = True
                                        text = text[start_idx + 7:]
                                    else:
                                        # Check for partial <think> tag at end of text
                                        partial_match = ""
                                        for i in range(1, min(7, len(text) + 1)):
                                            if "<think>"[:i] == text[-i:]:
                                                partial_match = text[-i:]
                                        if partial_match:
                                            filtered += text[:-len(partial_match)]
                                            think_buffer = partial_match
                                        else:
                                            filtered += text
                                        break

                            if filtered:
                                assistant_content_parts.append(filtered)
                                yield StreamChunk(type="text", content=filtered)
                        elif chunk.type == "tool_call":
                            emitted_output = True
                            tool_calls.append(chunk.tool_call)
                            yield chunk
                        elif chunk.type == "done":
                            # Flush any remaining think_buffer as normal text
                            # (handles unclosed <think> tags at end of stream)
                            if think_buffer:
                                assistant_content_parts.append(think_buffer)
                                yield StreamChunk(type="text", content=think_buffer)
                                think_buffer = ""
                            in_think_block = False

                            usage = chunk.usage or {}
                            round_llm_config = candidate_llm_config
                            candidate_used_fallback = is_fallback
                            candidate_backend_name = candidate_llm_config["backend"]
                            emitted_output = True
                            # Don't yield "done" yet — we may loop for tool results
                        elif chunk.type == "error":
                            raise RuntimeError(chunk.error)

                    if round_llm_config is not None:
                        await llm_manager.record_backend_success(backend_name)
                        break
                except Exception as e:
                    await llm_manager.record_backend_failure(backend_name)
                    candidate_error = e
                    if emitted_output:
                        yield StreamChunk(type="error", error=f"LLM error: {str(e)}")
                        return
                    continue

            if round_llm_config is None:
                await hook_registry.fire("on_error", {
                    "conversation_id": conversation_id,
                    "error": str(candidate_error),
                    "stage": "llm_call",
                })
                yield StreamChunk(type="error", error=f"No LLM available: {str(candidate_error)}")
                return

            # Update tracking state
            llm_config = round_llm_config
            all_assistant_content_parts.extend(assistant_content_parts)
            for k, v in usage.items():
                total_usage[k] = total_usage.get(k, 0) + v

            await hook_registry.fire("post_llm_call", {
                "conversation_id": conversation_id,
                "backend": candidate_backend_name,
                "model": llm_config["model"],
                "usage": usage,
                "tool_calls_count": len(tool_calls),
            })

            # Save assistant response
            assistant_content = "".join(assistant_content_parts)
            assistant_msg_doc = {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": assistant_content,
                "model": llm_config["model"],
                "tokens": usage,
                "created_at": datetime.now(timezone.utc),
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
                    "$set": {"updated_at": datetime.now(timezone.utc)},
                    "$inc": {
                        "stats.message_count": 1,
                        "stats.total_tokens": usage.get("input_tokens", 0)
                        + usage.get("output_tokens", 0),
                        "stats.tool_calls": len(tool_calls),
                    },
                },
            )

            # Capture loop variables by value for the background task
            _cfg = llm_config
            _usage = dict(usage)
            _backend = candidate_backend_name
            _fallback = candidate_used_fallback

            async def _record_usage(cfg=_cfg, u=_usage, bk=_backend, fb=_fallback):
                try:
                    await self.usage_repo.record(
                        model=cfg["model"],
                        source="conversation",
                        input_tokens=u.get("input_tokens", 0),
                        output_tokens=u.get("output_tokens", 0),
                        agent_slug=agent.get("slug"),
                        conversation_id=conversation_id,
                        metadata={
                            "backend": bk,
                            "fallback": fb,
                        },
                    )
                except Exception as e:
                    logger.error("Failed to record usage: %s", e)

            asyncio.create_task(_record_usage())

            # If no tool calls, we're done — break out of the tool-call loop
            if not tool_calls or not self.tool_router:
                break

            # Execute tool calls (with steering message checkpoints)
            tool_results_for_llm = []
            interrupted = False
            for tool_call in tool_calls:
                # Check for steering messages between tool calls
                steering_messages = steering_queue.drain(conversation_id)
                if steering_messages:
                    for sm in steering_messages:
                        if sm.priority == "interrupt":
                            # Interrupt: stop tool execution, inject steering as user message
                            steering_msg_doc = {
                                "id": str(uuid.uuid4()),
                                "role": "user",
                                "content": f"[STEERING INTERRUPT] {sm.content}",
                                "created_at": sm.created_at,
                                "memory_processed": False,
                            }
                            await self.db.conversations.update_one(
                                {"_id": ObjectId(conversation_id)},
                                {
                                    "$push": {"messages": steering_msg_doc},
                                    "$set": {"updated_at": datetime.now(timezone.utc)},
                                    "$inc": {"stats.message_count": 1},
                                },
                            )
                            yield StreamChunk(
                                type="text",
                                content=f"\n[Steering interrupt received: {sm.content}]\n",
                            )
                            yield StreamChunk(type="done", usage=total_usage)
                            return
                        else:
                            # Normal: append as context note, continue execution
                            yield StreamChunk(
                                type="text",
                                content=f"\n[User note: {sm.content}]\n",
                            )

                # Execute the tool with lifecycle hooks
                await hook_registry.fire("pre_tool_call", {
                    "conversation_id": conversation_id,
                    "tool_name": tool_call.name,
                    "arguments": tool_call.arguments,
                })

                result = await self.tool_router.execute_tool(
                    tool_name=tool_call.name,
                    arguments=tool_call.arguments,
                )

                await hook_registry.fire("post_tool_call", {
                    "conversation_id": conversation_id,
                    "tool_name": tool_call.name,
                    "status": result.status.value,
                    "duration_ms": result.duration_ms,
                })

                # Build tool result content
                tool_content = str(result.output) if result.output else (result.error or "")

                # Save tool result message
                tool_result_msg = {
                    "id": str(uuid.uuid4()),
                    "role": "tool",
                    "content": tool_content,
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.name,
                    "status": result.status.value,
                    "created_at": datetime.now(timezone.utc),
                    "memory_processed": False,
                }

                await self.db.conversations.update_one(
                    {"_id": ObjectId(conversation_id)},
                    {
                        "$push": {"messages": tool_result_msg},
                        "$set": {"updated_at": datetime.now(timezone.utc)},
                        "$inc": {"stats.message_count": 1},
                    },
                )

                # Yield tool result to client
                yield StreamChunk(
                    type="text",
                    content=f"\n[Tool {tool_call.name}: {result.status.value}]\n",
                )

                # Collect for LLM follow-up
                tool_results_for_llm.append({
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.name,
                    "content": tool_content,
                })

            # Append tool call + result messages to the LLM message list
            # so the next iteration has context
            messages.append(Message(
                role="assistant",
                content=assistant_content,
                tool_calls=[
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in tool_calls
                ],
            ))
            for tr in tool_results_for_llm:
                messages.append(Message(
                    role="tool",
                    content=tr["content"],
                    tool_call_id=tr["tool_call_id"],
                ))
            # Loop continues — LLM will be called again with tool results

        # After the tool-call loop, emit the final "done" event
        usage = total_usage
        yield StreamChunk(type="done", usage=usage)

        # 10. Queue memory extraction if enabled
        if agent.get("memory_config", {}).get("auto_extract", False):
            if background_tasks:
                # Use FastAPI BackgroundTasks for proper lifecycle management
                async def run_extraction():
                    try:
                        await hook_registry.fire("pre_memory_extract", {
                            "conversation_id": conversation_id,
                        })
                        count = await self.memory_extractor.extract_from_conversation(
                            conversation_id,
                            llm_backend=llm_config["backend"],
                            llm_model=llm_config["model"],
                            private=is_private,
                        )
                        await hook_registry.fire("post_memory_extract", {
                            "conversation_id": conversation_id,
                            "memories_extracted": count,
                        })
                        logger.info("Extracted %d memories from conversation %s", count, conversation_id)
                    except Exception as e:
                        logger.error("Memory extraction error: %s", e)

                background_tasks.add_task(run_extraction)
            else:
                # Fallback to asyncio.create_task if BackgroundTasks not available
                # This can happen in non-HTTP contexts (e.g., CLI, tests)
                try:
                    asyncio.create_task(
                        self.memory_extractor.extract_from_conversation(
                            conversation_id,
                            llm_backend=llm_config["backend"],
                            llm_model=llm_config["model"],
                            private=is_private,
                        )
                    )
                except Exception as e:
                    logger.error("Failed to queue memory extraction: %s", e)

        short_term_messages = agent.get("memory_config", {}).get("short_term_messages", 20)

        async def run_summary_update():
            try:
                summary_adapter = llm_manager.get_adapter(
                    llm_config["backend"],
                    llm_config["model"],
                )
                await maybe_update_conversation_summary(
                    self.db,
                    conversation_id,
                    llm=summary_adapter,
                    short_term_messages=short_term_messages,
                )
            except Exception as e:
                logger.error("Error updating conversation summary: %s", e)

        if background_tasks:
            background_tasks.add_task(run_summary_update)
        else:
            try:
                asyncio.create_task(run_summary_update())
            except Exception as e:
                logger.error("Failed to queue summary update: %s", e)

        # Fire post_message hook (fire-and-forget to not block response)
        asyncio.create_task(hook_registry.fire("post_message", {
            "conversation_id": conversation_id,
            "assistant_content": assistant_content,
            "usage": usage,
            "tool_calls_count": len(tool_calls),
            "backend": candidate_backend_name,
            "model": llm_config["model"],
        }))

    async def process_message_with_ooda(
        self,
        conversation_id: str,
        user_message: str,
        ooda_config: dict,
        background_tasks=None,
    ) -> AsyncIterator[StreamChunk]:
        """
        Process a message with OODA self-correction loop.

        Buffers the first response, evaluates quality, retries if below threshold.
        Only the final accepted response is yielded to the client.
        """
        threshold = ooda_config.get("threshold", 0.7)
        max_retries = ooda_config.get("max_retries", 2)
        backend = ooda_config.get("backend", "llamacpp")
        model = ooda_config.get("model", "default")

        ooda = OODALoop(threshold=threshold, max_retries=max_retries)

        async def generate_response() -> str:
            """Buffer a full response from the orchestrator."""
            parts = []
            async for chunk in self.process_message(
                conversation_id, user_message, stream=False, background_tasks=background_tasks
            ):
                if chunk.type == "text":
                    parts.append(chunk.content)
                elif chunk.type == "error":
                    return f"[Error: {chunk.error}]"
            return "".join(parts)

        result = await ooda.process_with_ooda(
            question=user_message,
            generate_fn=generate_response,
            backend=backend,
            model=model,
        )

        # Yield the accepted response
        yield StreamChunk(type="text", content=result.content)
        yield StreamChunk(
            type="done",
            usage={
                "ooda_score": result.score,
                "ooda_attempts": result.attempts,
                "ooda_accepted": result.accepted,
                "ooda_feedback": result.feedback,
            },
        )
