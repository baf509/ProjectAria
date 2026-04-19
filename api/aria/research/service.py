"""
ARIA - Research Service

Purpose: Background recursive research orchestration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.core.prompts import load_prompt
from aria.core.resilience import retry_async
from aria.db.usage import UsageRepo
from aria.llm.base import Message
from aria.llm.manager import llm_manager
from aria.memory.long_term import LongTermMemory
from aria.research.models import Learning, ResearchConfig
from aria.research.search import SearchResult, get_search_provider
from aria.tasks.runner import TaskRunner
from aria.tools.base import ToolStatus
from aria.tools.builtin.search_agent import SearchAgentTool
from aria.tools.builtin.web import WebTool

logger = logging.getLogger(__name__)


class ResearchService:
    """Runs background research tasks and persists results."""

    def __init__(self, db: AsyncIOMotorDatabase, task_runner: TaskRunner):
        self.db = db
        self.task_runner = task_runner
        self.usage_repo = UsageRepo(db)
        self.long_term_memory = LongTermMemory(db)
        self.search_provider = get_search_provider()
        self.web_tool = WebTool(timeout_seconds=20, max_response_size=512 * 1024)
        self._search_agent_tool: Optional[SearchAgentTool] = None
        self.task_runner.register_recovery_handler("research", self._recover_research_task)

    async def start_research(
        self,
        query: str,
        depth: int = 2,
        breadth: int = 3,
        model: Optional[str] = None,
        backend: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> dict:
        """Create a research record and enqueue background execution."""
        config = await self._build_config(
            query=query,
            depth=depth,
            breadth=breadth,
            model=model,
            backend=backend,
            conversation_id=conversation_id,
        )
        research_id = str(uuid4())
        now = datetime.now(timezone.utc)
        doc = {
            "_id": research_id,
            "query": config.query,
            "status": "queued",
            "task_id": "pending",
            "backend": config.llm_backend,
            "model": config.llm_model,
            "depth": config.depth,
            "breadth": config.breadth,
            "conversation_id": ObjectId(config.conversation_id) if config.conversation_id else None,
            "progress": {
                "current_depth": 0,
                "max_depth": config.depth,
                "queries_completed": 0,
                "queries_total": 1,
                "learnings_count": 0,
            },
            "sources": [],
            "learnings": [],
            "report_text": None,
            "total_tokens": 0,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        await self.db.research_runs.insert_one(doc)

        task_id = await self.task_runner.submit_task(
            name=f"research:{query[:80]}",
            coroutine_factory=lambda: self._run_research(research_id, config),
            metadata={"research_id": research_id, "query": query, "task_kind": "research"},
        )
        await self.db.research_runs.update_one(
            {"_id": research_id},
            {"$set": {"task_id": task_id, "updated_at": datetime.now(timezone.utc)}},
        )
        return {"research_id": research_id, "task_id": task_id}

    async def _recover_research_task(self, metadata: dict) -> object:
        research_id = metadata["research_id"]
        run = await self.get_run(research_id)
        if not run:
            raise RuntimeError(f"Research run {research_id} no longer exists")
        config = ResearchConfig(
            query=run["query"],
            depth=run.get("depth", 2),
            breadth=run.get("breadth", 3),
            llm_backend=run.get("backend", settings.research_default_backend),
            llm_model=run.get("model", settings.research_default_model),
            conversation_id=str(run["conversation_id"]) if run.get("conversation_id") else None,
        )
        return await self._run_research(research_id, config)

    async def list_runs(self) -> list[dict]:
        return await self.db.research_runs.find().sort("created_at", -1).to_list(length=200)

    async def get_run(self, research_id: str) -> Optional[dict]:
        return await self.db.research_runs.find_one({"_id": research_id})

    async def get_report(self, research_id: str) -> Optional[dict]:
        return await self.db.research_runs.find_one(
            {"_id": research_id},
            {"report_text": 1, "query": 1, "status": 1, "completed_at": 1},
        )

    async def get_learnings(self, research_id: str) -> list[dict]:
        run = await self.db.research_runs.find_one({"_id": research_id}, {"learnings": 1})
        if not run:
            return []
        return run.get("learnings", [])

    async def _build_config(
        self,
        *,
        query: str,
        depth: int,
        breadth: int,
        model: Optional[str],
        backend: Optional[str],
        conversation_id: Optional[str],
    ) -> ResearchConfig:
        if model and not backend:
            backend = settings.research_default_backend
        if backend and not model:
            model = settings.research_default_model

        if not backend or not model:
            agent = await self.db.agents.find_one({"is_default": True})
            if agent:
                backend = backend or agent["llm"]["backend"]
                model = model or agent["llm"]["model"]

        return ResearchConfig(
            query=query,
            depth=depth,
            breadth=breadth,
            llm_backend=backend or settings.research_default_backend,
            llm_model=model or settings.research_default_model,
            conversation_id=conversation_id,
        )

    async def _run_research(self, research_id: str, config: ResearchConfig) -> dict:
        started_at = datetime.now(timezone.utc)
        await self._update_run(
            research_id,
            status="running",
            progress={
                "current_depth": 0,
                "max_depth": config.depth,
                "queries_completed": 0,
                "queries_total": 1,
                "learnings_count": 0,
            },
        )
        run_doc = await self.get_run(research_id)
        task_id = run_doc.get("task_id") if run_doc and run_doc.get("task_id") not in {None, "pending"} else None
        if task_id is None:
            for _ in range(40):
                await asyncio.sleep(0.05)
                run_doc = await self.get_run(research_id)
                task_id = run_doc.get("task_id") if run_doc and run_doc.get("task_id") not in {None, "pending"} else None
                if task_id is not None:
                    break
            if task_id is None:
                logger.warning("Could not resolve task_id for research run %s, progress updates will be skipped", research_id)
        run_state = {
            "queries_completed": 0,
            "queries_total": 1,
            "learnings": [],
            "sources": [],
            "total_tokens": 0,
        }

        try:
            await self._research_branch(
                research_id=research_id,
                task_id=task_id,
                config=config,
                branch_query=config.query,
                remaining_depth=config.depth,
                run_state=run_state,
            )

            report_text = await self._synthesize_report(config, run_state["learnings"], run_state)
            await self._persist_report_memories(config, run_state["learnings"], report_text)

            completed_at = datetime.now(timezone.utc)
            duration = (completed_at - started_at).total_seconds()
            result = {
                "research_id": research_id,
                "learnings_count": len(run_state["learnings"]),
                "total_tokens": run_state["total_tokens"],
                "duration_seconds": duration,
            }
            await self._update_run(
                research_id,
                status="completed",
                progress={
                    "current_depth": config.depth,
                    "max_depth": config.depth,
                    "queries_completed": run_state["queries_completed"],
                    "queries_total": run_state["queries_total"],
                    "learnings_count": len(run_state["learnings"]),
                },
                extra_updates={
                    "report_text": report_text,
                    "learnings": [asdict(learning) for learning in run_state["learnings"]],
                    "sources": run_state["sources"],
                    "total_tokens": run_state["total_tokens"],
                    "completed_at": completed_at,
                },
            )
            if task_id:
                await self.task_runner.update_task(
                    task_id,
                    progress=100,
                    metadata={"research_id": research_id, "learnings_count": len(run_state["learnings"])},
                    result=result,
                )
            return result
        except Exception:
            logger.exception("Research run failed", extra={"research_id": research_id})
            await self._update_run(research_id, status="failed")
            if task_id:
                await self.task_runner.update_task(task_id, status="failed", error="Research run failed")
            raise

    async def _research_branch(
        self,
        *,
        research_id: str,
        task_id: Optional[str],
        config: ResearchConfig,
        branch_query: str,
        remaining_depth: int,
        run_state: dict,
    ) -> None:
        depth_found = config.depth - remaining_depth
        if config.llm_backend == "context1":
            results, source_docs = await self._agentic_gather(branch_query, config.breadth)
        else:
            results = await self.search_provider.search(branch_query, max_results=min(config.breadth + 1, 5))
            source_docs = await self._fetch_sources(results[: config.breadth])
        learnings = await self._extract_learnings(
            config=config,
            branch_query=branch_query,
            depth_found=depth_found,
            sources=source_docs,
            run_state=run_state,
        )
        run_state["learnings"].extend(learnings)
        run_state["sources"].extend(result.to_dict() for result in results[: config.breadth])
        run_state["queries_completed"] += 1

        await self._update_progress(
            research_id=research_id,
            task_id=task_id,
            run_state=run_state,
            config=config,
            current_depth=depth_found,
        )

        if remaining_depth <= 1:
            return

        followups = await self._generate_followup_queries(
            config=config,
            branch_query=branch_query,
            depth_found=depth_found,
            breadth=max(1, config.breadth // 2),
            run_state=run_state,
        )
        if not followups:
            return

        selected = followups[: max(1, config.breadth // 2)]
        run_state["queries_total"] += len(selected)
        for followup in selected:
            await self._research_branch(
                research_id=research_id,
                task_id=task_id,
                config=config,
                branch_query=followup,
                remaining_depth=remaining_depth - 1,
                run_state=run_state,
            )

    async def _generate_followup_queries(
        self,
        *,
        config: ResearchConfig,
        branch_query: str,
        depth_found: int,
        breadth: int,
        run_state: dict,
    ) -> list[str]:
        adapter = llm_manager.get_adapter(config.llm_backend, config.llm_model)
        prompt = load_prompt("research_query",
            query=config.query,
            branch_query=branch_query,
            depth=depth_found,
            breadth=breadth,
        )
        response, usage = await self._complete(
            adapter=adapter,
            messages=[Message(role="user", content=prompt)],
            temperature=0.7,
            max_tokens=512,
            source="research_query_generation",
            conversation_id=config.conversation_id,
            model=config.llm_model,
            backend=config.llm_backend,
        )
        run_state["total_tokens"] += usage.get("total_tokens", 0)
        parsed = self._parse_json(response)
        return [item for item in parsed if isinstance(item, str)][:breadth]

    async def _extract_learnings(
        self,
        *,
        config: ResearchConfig,
        branch_query: str,
        depth_found: int,
        sources: list[dict],
        run_state: dict,
    ) -> list[Learning]:
        adapter = llm_manager.get_adapter(config.llm_backend, config.llm_model)
        source_text = "\n\n".join(
            f"URL: {source['url']}\nTITLE: {source['title']}\nSNIPPET: {source['snippet']}\nCONTENT: {source['content'][:4000]}"
            for source in sources
        )
        prompt = load_prompt("research_learnings",
            query=config.query,
            branch_query=branch_query,
            depth=depth_found,
            sources=source_text or "No sources fetched successfully.",
        )
        response, usage = await self._complete(
            adapter=adapter,
            messages=[Message(role="user", content=prompt)],
            temperature=0.3,
            max_tokens=1400,
            source="research_learning_extraction",
            conversation_id=config.conversation_id,
            model=config.llm_model,
            backend=config.llm_backend,
        )
        run_state["total_tokens"] += usage.get("total_tokens", 0)
        parsed = self._parse_json(response)
        learnings = []
        for item in parsed:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            learnings.append(
                Learning(
                    content=item["content"].strip(),
                    source_url=item.get("source_url"),
                    confidence=float(item.get("confidence", 0.5)),
                    depth_found=depth_found,
                    query_context=branch_query,
                )
            )
        return self._dedupe_learnings(learnings)

    async def _synthesize_report(
        self,
        config: ResearchConfig,
        learnings: list[Learning],
        run_state: dict,
    ) -> str:
        adapter = llm_manager.get_adapter(config.llm_backend, config.llm_model)
        learnings_text = "\n".join(
            f"- {learning.content} (confidence={learning.confidence:.2f}, source={learning.source_url or 'n/a'})"
            for learning in learnings[:60]
        )
        prompt = load_prompt("research_synthesis", query=config.query, learnings=learnings_text or "- No learnings collected.")
        response, usage = await self._complete(
            adapter=adapter,
            messages=[Message(role="user", content=prompt)],
            temperature=0.5,
            max_tokens=2048,
            source="research_synthesis",
            conversation_id=config.conversation_id,
            model=config.llm_model,
            backend=config.llm_backend,
        )
        run_state["total_tokens"] += usage.get("total_tokens", 0)
        return response.strip()

    async def _persist_report_memories(
        self,
        config: ResearchConfig,
        learnings: list[Learning],
        report_text: str,
    ) -> None:
        tags = self._query_tags(config.query)
        for learning in learnings[:25]:
            await self.long_term_memory.create_memory(
                content=learning.content,
                content_type="fact",
                categories=["research", *tags],
                importance=min(1.0, 0.5 + (learning.confidence * 0.4)),
                confidence=learning.confidence,
                source={
                    "type": "research",
                    "query": config.query,
                    "source_url": learning.source_url,
                },
            )
        await self.long_term_memory.create_memory(
            content=f"Research report for '{config.query}': {report_text[:3000]}",
            content_type="document",
            categories=["research", *tags],
            importance=0.8,
            confidence=0.75,
            source={"type": "research_report", "query": config.query},
        )

    async def _agentic_gather(
        self, query: str, breadth: int
    ) -> tuple[list[SearchResult], list[dict]]:
        """Retrieve sources via the context-1 search agent over memory/web/files."""
        if self._search_agent_tool is None:
            self._search_agent_tool = SearchAgentTool(self.db, self.long_term_memory)
        tool_result = await self._search_agent_tool.execute({
            "query": query,
            "max_docs": max(breadth, 3),
        })
        if tool_result.status != ToolStatus.SUCCESS:
            logger.warning("search_agent failed (%s); falling back to web provider", tool_result.error)
            fallback = await self.search_provider.search(query, max_results=min(breadth + 1, 5))
            return fallback, await self._fetch_sources(fallback[:breadth])

        documents = (tool_result.output or {}).get("documents", []) or []
        results: list[SearchResult] = []
        source_docs: list[dict] = []
        for doc in documents[:breadth]:
            url = doc.get("url") or f"aria://{doc.get('id', 'unknown')}"
            title = doc.get("title") or doc.get("id") or "document"
            content = doc.get("content") or ""
            snippet = content[:400]
            results.append(SearchResult(title=title, url=url, snippet=snippet))
            source_docs.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "content": content,
            })
        return results, source_docs

    async def _fetch_sources(self, results: list[SearchResult]) -> list[dict]:
        docs = []
        for result in results:
            content = ""
            fetch_result = await self.web_tool.execute({"url": result.url, "timeout": 20})
            if fetch_result.status.value == "success":
                output = fetch_result.output or {}
                content = str(output.get("content", ""))[:12000]
            docs.append(
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                    "content": self._strip_html(content),
                }
            )
        return docs

    async def _complete(
        self,
        *,
        adapter,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        source: str,
        conversation_id: Optional[str],
        model: str,
        backend: str,
    ) -> tuple[str, dict]:
        # Use ClaudeRunner for subscription tokens when available
        if settings.use_claude_runner and ClaudeRunner.is_available():
            # Combine messages into a single prompt for the CLI
            prompt_parts = []
            for msg in messages:
                if msg.role == "system":
                    prompt_parts.append(msg.content)
                else:
                    prompt_parts.append(msg.content)
            prompt = "\n\n".join(prompt_parts)
            runner = ClaudeRunner(timeout_seconds=settings.claude_runner_timeout_seconds)
            result = await runner.run(prompt)
            if result:
                return result, {}

        # Fall back to API tokens
        content, _, usage = await retry_async(
            lambda: adapter.complete(messages=messages, temperature=temperature, max_tokens=max_tokens),
            retries=3,
            base_delay=1.0,
        )
        if usage:
            await self.usage_repo.record(
                model=model,
                source=source,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                conversation_id=conversation_id,
                metadata={"backend": backend},
            )
        return content, usage or {}

    async def _update_progress(
        self,
        *,
        research_id: str,
        task_id: Optional[str],
        run_state: dict,
        config: ResearchConfig,
        current_depth: int,
    ) -> None:
        progress_pct = int((run_state["queries_completed"] / max(run_state["queries_total"], 1)) * 90)
        progress = {
            "current_depth": current_depth,
            "max_depth": config.depth,
            "queries_completed": run_state["queries_completed"],
            "queries_total": run_state["queries_total"],
            "learnings_count": len(run_state["learnings"]),
        }
        await self._update_run(research_id, progress=progress)
        if task_id:
            await self.task_runner.update_task(
                task_id,
                progress=progress_pct,
                metadata={
                    "queries_completed": run_state["queries_completed"],
                    "queries_total": run_state["queries_total"],
                    "learnings_count": len(run_state["learnings"]),
                },
            )

    async def _update_run(
        self,
        research_id: str,
        *,
        status: Optional[str] = None,
        progress: Optional[dict] = None,
        extra_updates: Optional[dict] = None,
    ) -> None:
        updates = {"updated_at": datetime.now(timezone.utc)}
        if status is not None:
            updates["status"] = status
        if progress is not None:
            updates["progress"] = progress
        if extra_updates:
            updates.update(extra_updates)
        await self.db.research_runs.update_one({"_id": research_id}, {"$set": updates})

    def _parse_json(self, value: str) -> list | dict:
        cleaned = value.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON from LLM response: %.200s", cleaned)
            return []
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return parsed.get("items", [])
        return []

    def _dedupe_learnings(self, learnings: list[Learning]) -> list[Learning]:
        deduped = []
        seen = set()
        for learning in learnings:
            key = learning.content.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(learning)
        return deduped

    def _strip_html(self, value: str) -> str:
        value = re.sub(r"<script.*?</script>", " ", value, flags=re.DOTALL | re.IGNORECASE)
        value = re.sub(r"<style.*?</style>", " ", value, flags=re.DOTALL | re.IGNORECASE)
        value = re.sub(r"<[^>]+>", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def _query_tags(self, query: str) -> list[str]:
        return [token for token in re.findall(r"[a-z0-9]+", query.lower())[:6] if len(token) > 2]
