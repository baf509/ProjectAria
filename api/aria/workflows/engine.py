"""
ARIA - Workflow Engine

Purpose: Execute simple multi-step workflows.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
from typing import Any, Optional
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.agents.session import CodingSessionManager
from aria.core.orchestrator import Orchestrator
from aria.notifications.service import NotificationService
from aria.research.service import ResearchService
from aria.tasks.runner import TaskRunner
from aria.tools.router import ToolRouter


class WorkflowEngine:
    """Persist and execute workflow definitions."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        task_runner: TaskRunner,
        tool_router: ToolRouter,
        notification_service: NotificationService,
        research_service: ResearchService,
        coding_manager: CodingSessionManager,
    ):
        self.db = db
        self.task_runner = task_runner
        self.tool_router = tool_router
        self.notification_service = notification_service
        self.research_service = research_service
        self.coding_manager = coding_manager
        self.task_runner.register_recovery_handler("workflow", self._recover_run)

    async def list_workflows(self) -> list[dict]:
        return await self.db.workflows.find().sort("created_at", -1).to_list(length=200)

    async def get_workflow(self, workflow_id: str) -> Optional[dict]:
        return await self.db.workflows.find_one({"_id": workflow_id})

    async def create_workflow(self, body: dict) -> dict:
        workflow_id = str(uuid4())
        now = datetime.now(timezone.utc)
        doc = {
            "_id": workflow_id,
            "name": body["name"],
            "description": body.get("description", ""),
            "steps": body.get("steps", []),
            "tags": body.get("tags", []),
            "created_at": now,
            "updated_at": now,
        }
        await self.db.workflows.insert_one(doc)
        return doc

    async def run_workflow(self, workflow_id: str, dry_run: bool = False) -> dict:
        workflow = await self.get_workflow(workflow_id)
        if not workflow:
            raise ValueError("Workflow not found")

        run_id = str(uuid4())
        now = datetime.now(timezone.utc)
        run_doc = {
            "_id": run_id,
            "workflow_id": workflow_id,
            "status": "queued",
            "dry_run": dry_run,
            "step_results": [],
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "task_id": "pending",
        }
        await self.db.workflow_runs.insert_one(run_doc)

        task_id = await self.task_runner.submit_task(
            name=f"workflow:{workflow['name']}",
            coroutine_factory=lambda: self._execute_run(run_id, workflow, dry_run),
            metadata={"task_kind": "workflow", "workflow_run_id": run_id, "workflow_id": workflow_id},
        )
        await self.db.workflow_runs.update_one(
            {"_id": run_id},
            {"$set": {"task_id": task_id, "updated_at": datetime.now(timezone.utc)}},
        )
        return {"run_id": run_id, "task_id": task_id}

    async def get_run(self, run_id: str) -> Optional[dict]:
        return await self.db.workflow_runs.find_one({"_id": run_id})

    async def _recover_run(self, metadata: dict) -> object:
        run = await self.get_run(metadata["workflow_run_id"])
        workflow = await self.get_workflow(metadata["workflow_id"])
        if not run or not workflow:
            raise RuntimeError("Workflow run or definition missing")
        return await self._execute_run(run["_id"], workflow, run.get("dry_run", False))

    async def _execute_run(self, run_id: str, workflow: dict, dry_run: bool) -> dict:
        workflow = {**workflow, "_active_run_id": run_id}
        total_steps = len(workflow.get("steps", []))
        await self.db.workflow_runs.update_one(
            {"_id": run_id},
            {"$set": {"status": "running", "updated_at": datetime.now(timezone.utc)}},
        )
        results: list[dict[str, Any]] = []
        try:
            for index, step in enumerate(workflow.get("steps", [])):
                action = step["action"]
                depends_on = step.get("depends_on", [])
                self._validate_dependencies(index, depends_on)

                step_record = await self._execute_step(
                    workflow=workflow,
                    index=index,
                    step=step,
                    results=results,
                    dry_run=dry_run,
                )
                results.append(step_record)
                await self._persist_run_progress(run_id, results, total_steps)
                if step_record.get("status") == "failed":
                    raise RuntimeError(step_record.get("error") or f"Workflow step {index} failed")

            await self.db.workflow_runs.update_one(
                {"_id": run_id},
                {"$set": {"status": "completed", "updated_at": datetime.now(timezone.utc), "completed_at": datetime.now(timezone.utc)}},
            )
            return {"run_id": run_id, "step_results": results}
        except Exception as exc:
            await self.db.workflow_runs.update_one(
                {"_id": run_id},
                {
                    "$set": {
                        "status": "failed",
                        "error": str(exc),
                        "step_results": results,
                        "updated_at": datetime.now(timezone.utc),
                        "completed_at": datetime.now(timezone.utc),
                    }
                },
            )
            raise

    async def _execute_step(
        self,
        *,
        workflow: dict,
        index: int,
        step: dict,
        results: list[dict[str, Any]],
        dry_run: bool,
    ) -> dict[str, Any]:
        action = step["action"]
        depends_on = step.get("depends_on", [])
        skip_reason = self._get_skip_reason(depends_on, results)
        if skip_reason:
            return {
                "index": index,
                "action": action,
                "depends_on": depends_on,
                "status": "skipped",
                "skipped": True,
                "skip_reason": skip_reason,
                "result": None,
            }

        params = self._render_params(
            step.get("params", {}),
            results,
            {
                "run_id": workflow.get("_active_run_id"),
                "workflow_id": workflow.get("_id"),
                "workflow_name": workflow.get("name"),
            },
        )

        try:
            if dry_run:
                result = {"dry_run": True, "action": action, "params": params}
            else:
                result = await self._perform_action(workflow, action, params)
            return {
                "index": index,
                "action": action,
                "depends_on": depends_on,
                "status": "completed",
                "result": result,
            }
        except Exception as exc:
            return {
                "index": index,
                "action": action,
                "depends_on": depends_on,
                "status": "failed",
                "error": str(exc),
                "result": None,
            }

    async def _perform_action(self, workflow: dict, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "wait":
            seconds = float(params.get("seconds", 1))
            await asyncio.sleep(seconds)
            return {"waited_seconds": seconds}
        if action == "condition":
            return self._evaluate_condition(params)
        if action == "notify":
            return await self.notification_service.notify(
                source=f"workflow:{workflow['name']}",
                event_type=params.get("event_type", "info"),
                detail=params.get("detail", ""),
                recipient=params.get("recipient"),
            )
        if action == "tool":
            tool_name = params["tool_name"]
            arguments = params.get("arguments", {})
            tool_result = await self.tool_router.execute_tool(tool_name=tool_name, arguments=arguments)
            return {"status": tool_result.status.value, "output": tool_result.output, "error": tool_result.error}
        if action == "research":
            return await self.research_service.start_research(
                query=params["query"],
                depth=int(params.get("depth", 2)),
                breadth=int(params.get("breadth", 3)),
                conversation_id=params.get("conversation_id"),
            )
        if action == "code_session":
            session = await self.coding_manager.start_session(
                workspace=params["workspace"],
                backend=params.get("backend"),
                prompt=params["prompt"],
                model=params.get("model"),
                branch=params.get("branch"),
                conversation_id=params.get("conversation_id"),
            )
            return {"session_id": session["_id"], "workspace": session["workspace"], "backend": session["backend"]}
        if action == "prompt":
            return await self._run_prompt_action(workflow, params)
        raise ValueError(f"Unsupported workflow action: {action}")

    async def _run_prompt_action(self, workflow: dict, params: dict[str, Any]) -> dict[str, Any]:
        orchestrator = Orchestrator(self.db, self.tool_router, task_runner=self.task_runner, coding_manager=self.coding_manager)
        agent = await self.db.agents.find_one({"is_default": True})
        if not agent:
            agent = await self.db.agents.find_one({}, sort=[("created_at", 1)])
        if not agent:
            raise RuntimeError("No agent available for workflow prompt action")
        now = datetime.now(timezone.utc)
        convo = {
            "agent_id": agent["_id"],
            "active_agent_id": None,
            "title": params.get("title") or f"Workflow {workflow['name']}",
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
            "tags": ["workflow"],
            "pinned": False,
            "stats": {"message_count": 0, "total_tokens": 0, "tool_calls": 0},
        }
        insert = await self.db.conversations.insert_one(convo)
        content_parts: list[str] = []
        async for chunk in orchestrator.process_message(str(insert.inserted_id), params["message"], stream=False):
            if chunk.type == "text" and chunk.content:
                content_parts.append(chunk.content)
        return {"conversation_id": str(insert.inserted_id), "response": "".join(content_parts)}

    def _evaluate_condition(self, params: dict[str, Any]) -> dict[str, Any]:
        source_value = params.get("value")
        expected = params.get("equals")
        not_equals = params.get("not_equals")
        contains = params.get("contains")
        matches = params.get("matches")
        exists = params.get("exists")

        passed = True
        if expected is not None:
            passed = source_value == expected
        if not_equals is not None:
            passed = passed and source_value != not_equals
        if contains is not None:
            passed = passed and str(contains) in str(source_value)
        if matches is not None:
            passed = passed and re.search(str(matches), str(source_value)) is not None
        if exists is not None:
            passed = passed and (source_value is not None) is bool(exists)
        return {"passed": passed, "value": source_value}

    def _validate_dependencies(self, index: int, depends_on: list[int]) -> None:
        non_int = [dep for dep in depends_on if not isinstance(dep, int)]
        if non_int:
            raise ValueError(f"Workflow step {index} has non-integer dependencies: {non_int}")
        invalid = [dep for dep in depends_on if dep < 0 or dep >= index]
        if invalid:
            raise ValueError(f"Workflow step {index} has invalid dependencies: {invalid}")

    def _get_skip_reason(self, depends_on: list[int], results: list[dict[str, Any]]) -> str | None:
        for dep in depends_on:
            if dep < 0 or dep >= len(results):
                return f"Dependency step {dep} is out of range"
            dependency = results[dep]
            if dependency.get("status") == "failed":
                return f"Dependency step {dep} failed"
            if dependency.get("status") == "skipped":
                return f"Dependency step {dep} was skipped"
            if dependency["action"] == "condition" and not (dependency.get("result") or {}).get("passed", False):
                return f"Condition step {dep} failed"
        return None

    async def _persist_run_progress(self, run_id: str, results: list[dict[str, Any]], total_steps: int) -> None:
        await self.db.workflow_runs.update_one(
            {"_id": run_id},
            {"$set": {"step_results": results, "updated_at": datetime.now(timezone.utc)}},
        )
        progress = 100 if total_steps == 0 else int((len(results) / total_steps) * 100)
        run = await self.get_run(run_id)
        task_id = (run or {}).get("task_id")
        if task_id and task_id != "pending":
            await self.task_runner.update_task(task_id, progress=progress)

    def _render_params(self, value: Any, results: list[dict[str, Any]], context: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: self._render_params(inner, results, context) for key, inner in value.items()}
        if isinstance(value, list):
            return [self._render_params(item, results, context) for item in value]
        if isinstance(value, str):
            rendered = re.sub(
                r"\{\{steps\.(\d+)(?:\.([a-zA-Z0-9_.-]+))?\}\}",
                lambda match: str(self._lookup_result(results, int(match.group(1)), match.group(2)) or ""),
                value,
            )
            return re.sub(
                r"\{\{workflow\.([a-zA-Z0-9_.-]+)\}\}",
                lambda match: str(context.get(match.group(1), "")),
                rendered,
            )
        return value

    def _lookup_result(self, results: list[dict[str, Any]], index: int, path: str | None) -> Any:
        if index < 0 or index >= len(results):
            return ""
        result = results[index].get("result")
        if not path:
            return result
        current: Any = result
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current
