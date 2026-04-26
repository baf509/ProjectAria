"""
ARIA - Task Extraction (ambient capture from conversations)

Mirrors MemoryExtractor in shape — runs as a background task right after the
orchestrator finishes a turn. Reads unprocessed messages, calls the LLM with
the task_extraction prompt, dedupes by content hash against open tasks, and
inserts proposed tasks / updates project activity. Honors the conversation's
`private` flag (no extraction on private chats).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.core.prompts import load_prompt
from aria.core.resilience import retry_async
from aria.db.usage import UsageRepo
from aria.llm.base import Message
from aria.llm.manager import llm_manager
from aria.planning.models import (
    ProjectCreateRequest,
    TaskCreateRequest,
    TaskSource,
)
from aria.planning.service import PlanningService

logger = logging.getLogger(__name__)


class TaskExtractor:
    """Ambient task/project signal extractor.

    Independent of MemoryExtractor — has its own per-message flag
    (`task_processed`) so the two extractors can be retried separately.
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.service = PlanningService(db)
        self.usage_repo = UsageRepo(db)

    async def extract_from_conversation(
        self,
        conversation_id: str,
        *,
        batch_size: int = 10,
        llm_backend: str = "llamacpp",
        llm_model: str = "default",
        private: bool = False,
    ) -> dict:
        """Process unprocessed messages and extract task/project signals.

        Returns counts: {tasks_proposed, tasks_deduped, projects_updated, new_projects}.
        Skips entirely on private conversations.
        """
        if private:
            logger.debug("Skipping task extraction for private conversation %s", conversation_id)
            return {
                "tasks_proposed": 0,
                "tasks_deduped": 0,
                "projects_updated": 0,
                "new_projects": 0,
            }

        conversation = await self.db.conversations.find_one({"_id": ObjectId(conversation_id)})
        if not conversation:
            return {
                "tasks_proposed": 0,
                "tasks_deduped": 0,
                "projects_updated": 0,
                "new_projects": 0,
            }

        unprocessed = [
            msg
            for msg in conversation.get("messages", [])
            if not msg.get("task_processed", False)
        ]
        if not unprocessed:
            return {
                "tasks_proposed": 0,
                "tasks_deduped": 0,
                "projects_updated": 0,
                "new_projects": 0,
            }

        totals = {
            "tasks_proposed": 0,
            "tasks_deduped": 0,
            "projects_updated": 0,
            "new_projects": 0,
        }
        for i in range(0, len(unprocessed), batch_size):
            batch = unprocessed[i : i + batch_size]
            counts = await self._extract_batch(conversation_id, batch, llm_backend, llm_model)
            for k, v in counts.items():
                totals[k] += v
        return totals

    async def _extract_batch(
        self,
        conversation_id: str,
        messages: list[dict],
        llm_backend: str,
        llm_model: str,
    ) -> dict:
        # Only consider USER turns for ambient capture — assistant content
        # rarely expresses user intent and is a major false-positive source.
        user_messages = [m for m in messages if m.get("role") == "user"]
        if not user_messages:
            await self._mark_processed(conversation_id, [m["id"] for m in messages if "id" in m])
            return {"tasks_proposed": 0, "tasks_deduped": 0, "projects_updated": 0, "new_projects": 0}

        messages_text = "\n\n".join(
            f"{m['role'].upper()}: {m.get('content', '')}" for m in user_messages
        )
        prompt = load_prompt("task_extraction", messages=messages_text)
        response = await self._call_llm(prompt, llm_backend, llm_model, conversation_id)
        if not response:
            await self._mark_processed(conversation_id, [m["id"] for m in messages if "id" in m])
            return {"tasks_proposed": 0, "tasks_deduped": 0, "projects_updated": 0, "new_projects": 0}

        try:
            payload = self._parse_response(response)
        except Exception as e:
            logger.warning("Failed to parse task extraction response: %s | response=%r", e, response[:500])
            return {"tasks_proposed": 0, "tasks_deduped": 0, "projects_updated": 0, "new_projects": 0}

        message_ids = [m["id"] for m in messages if "id" in m]
        counts = await self._apply(payload, conversation_id, message_ids)
        if message_ids:
            await self._mark_processed(conversation_id, message_ids)
        return counts

    async def _call_llm(
        self,
        prompt: str,
        llm_backend: str,
        llm_model: str,
        conversation_id: Optional[str] = None,
    ) -> Optional[str]:
        try:
            if settings.use_claude_runner and ClaudeRunner.is_available():
                runner = ClaudeRunner(timeout_seconds=settings.claude_runner_timeout_seconds)
                resp = await runner.run(prompt)
                if resp:
                    return resp
                logger.debug("ClaudeRunner returned no output for task extraction; falling back to API")
            return await self._extract_via_api(prompt, llm_backend, llm_model, conversation_id)
        except Exception as e:
            logger.error("Task extraction LLM call failed: %s", e)
            return None

    async def _extract_via_api(
        self,
        prompt: str,
        llm_backend: str,
        llm_model: str,
        conversation_id: Optional[str] = None,
    ) -> Optional[str]:
        adapter = llm_manager.get_adapter(llm_backend, llm_model)
        response, _, usage = await retry_async(
            lambda: adapter.complete(
                messages=[Message(role="user", content=prompt)],
                temperature=0.2,
                max_tokens=1024,
            ),
            retries=2,
            base_delay=1.0,
        )
        if usage and conversation_id:
            await self.usage_repo.record(
                model=llm_model,
                source="task_extraction",
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                conversation_id=conversation_id,
                metadata={"backend": llm_backend},
            )
        return response

    def _parse_response(self, response: str) -> dict:
        """Strip optional markdown fence, parse JSON, validate top-level shape."""
        cleaned = response.strip()
        if cleaned.startswith("```"):
            lines = [l for l in cleaned.split("\n") if not l.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("expected JSON object at top level")
        return {
            "tasks": data.get("tasks") or [],
            "project_updates": data.get("project_updates") or [],
            "new_projects": data.get("new_projects") or [],
        }

    async def _apply(self, payload: dict, conversation_id: str, message_ids: list[str]) -> dict:
        counts = {"tasks_proposed": 0, "tasks_deduped": 0, "projects_updated": 0, "new_projects": 0}

        # 1) New projects (explicit only — extractor must have emitted name+summary)
        slug_for_hint: dict[str, str] = {}
        for new_proj in payload["new_projects"][:3]:
            try:
                name = (new_proj.get("name") or "").strip()
                if not name:
                    continue
                # Skip if a project with this name/slug already exists
                existing = await self.service.fuzzy_find_project(name)
                if existing:
                    slug_for_hint[name.lower()] = existing.slug
                    continue
                proj = await self.service.create_project(
                    ProjectCreateRequest(
                        name=name,
                        summary=(new_proj.get("summary") or "")[:2000],
                    )
                )
                slug_for_hint[name.lower()] = proj.slug
                counts["new_projects"] += 1
            except Exception as e:
                logger.warning("Failed to create new project from extraction: %s", e)

        # 2) Project updates (only against existing projects)
        for upd in payload["project_updates"][:5]:
            try:
                hint = (upd.get("project_hint") or "").strip()
                if not hint:
                    continue
                proj = await self.service.fuzzy_find_project(hint)
                if not proj:
                    # Don't auto-create from a hint — too noisy. Drop silently.
                    continue
                note = (upd.get("status_note") or "").strip()
                if note:
                    await self.service.append_project_activity(
                        proj.id, source=f"conversation:{conversation_id}", note=note
                    )
                next_step = (upd.get("next_step") or "").strip()
                if next_step:
                    # Replace next_steps[0] with the latest signal — capped at 5,
                    # most recent first.
                    new_steps = [next_step] + [s for s in proj.next_steps if s != next_step]
                    await self.service.set_project_next_steps(proj.id, new_steps)
                counts["projects_updated"] += 1
            except Exception as e:
                logger.warning("Failed to apply project update: %s", e)

        # 3) Tasks
        for t in payload["tasks"][:5]:
            try:
                title = (t.get("title") or "").strip()
                confidence = float(t.get("confidence") or 0.0)
                if not title or confidence < 0.6:
                    continue

                # Resolve project hint to an existing project (don't auto-create)
                project_id: Optional[str] = None
                hint = (t.get("project_hint") or "").strip()
                if hint:
                    proj = await self.service.fuzzy_find_project(hint)
                    if proj:
                        project_id = proj.id

                # Hash-based dedup against open tasks
                from aria.planning.service import _content_hash  # local import keeps service deps tidy
                ch = _content_hash(title)
                existing = await self.service.find_open_task_by_hash(ch)
                if existing:
                    counts["tasks_deduped"] += 1
                    continue

                source = TaskSource(
                    type="conversation",
                    conversation_id=conversation_id,
                    message_ids=message_ids,
                    extracted_at=datetime.now(timezone.utc),
                    confidence=confidence,
                )
                await self.service.create_task(
                    TaskCreateRequest(
                        title=title,
                        notes=(t.get("notes") or None),
                        project_id=project_id,
                        status="proposed",  # ambient → always proposed in v1
                    ),
                    source=source,
                )
                counts["tasks_proposed"] += 1
            except Exception as e:
                logger.warning("Failed to insert proposed task: %s", e)

        return counts

    async def _mark_processed(self, conversation_id: str, message_ids: list[str]) -> None:
        if not message_ids:
            return
        try:
            await self.db.conversations.update_one(
                {"_id": ObjectId(conversation_id)},
                {"$set": {"messages.$[elem].task_processed": True}},
                array_filters=[{"elem.id": {"$in": message_ids}}],
            )
        except Exception as e:
            logger.error("Failed to mark messages task_processed: %s", e)
