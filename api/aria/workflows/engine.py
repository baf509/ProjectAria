"""
ARIA - Workflow Engine

Purpose: Execute multi-step workflows. A top-level linear DAG (conditions,
depends_on, {{steps.N.path}} interpolation) plus fan-out orchestration:
`parallel` (concurrent explicit sub-steps), `map` (one template over a list),
`code_session` with await:true (join a spawned sub-agent), and `synthesize`
(reduce prior results into one answer via an agent turn). Sub-step results nest
under the group as `results`/`records`, addressable as
{{steps.N.results.M.path}}.
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
        return await self._execute_run(
            run["_id"],
            workflow,
            run.get("dry_run", False),
            resume_results=run.get("step_results"),
        )

    async def _execute_run(
        self,
        run_id: str,
        workflow: dict,
        dry_run: bool,
        resume_results: list[dict[str, Any]] | None = None,
    ) -> dict:
        workflow = {**workflow, "_active_run_id": run_id}
        total_steps = len(workflow.get("steps", []))
        await self.db.workflow_runs.update_one(
            {"_id": run_id},
            {"$set": {"status": "running", "updated_at": datetime.now(timezone.utc)}},
        )
        # Recovery (e.g. after an aria-api restart mid-run) resumes from the
        # persisted step_results instead of replaying from step 0 -- otherwise
        # already-launched code_sessions get re-spawned and notify actions
        # re-fire. _persist_run_progress only ever records fully-completed
        # steps, so everything in resume_results is safe to skip outright.
        results: list[dict[str, Any]] = list(resume_results or [])
        start_index = len(results)
        try:
            for index, step in enumerate(workflow.get("steps", [])):
                if index < start_index:
                    continue
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
        scope: dict[str, Any] | None = None,
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

        # Fan-out actions orchestrate sub-steps and manage their own per-sub-step
        # param rendering (a `map` template's {{item}} isn't known at group
        # level), so they run BEFORE the wholesale _render_params below.
        if action in ("parallel", "map"):
            try:
                group_result = await self._execute_group(
                    workflow, action, step, results, dry_run
                )
                count = group_result.get("count", 0)
                failed = group_result.get("failed", 0)
                if count and failed == count:
                    return {
                        "index": index,
                        "action": action,
                        "depends_on": depends_on,
                        "status": "failed",
                        "error": f"All {count} {action} sub-step(s) failed",
                        "result": group_result,
                    }
                return {
                    "index": index,
                    "action": action,
                    "depends_on": depends_on,
                    "status": "completed",
                    "result": group_result,
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

        params = self._render_params(
            step.get("params", {}),
            results,
            self._ctx(workflow),
            scope=scope,
        )

        try:
            if dry_run:
                result = {"dry_run": True, "action": action, "params": params}
            else:
                result = await self._perform_action(workflow, action, params, results)
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

    def _ctx(self, workflow: dict) -> dict[str, Any]:
        return {
            "run_id": workflow.get("_active_run_id"),
            "workflow_id": workflow.get("_id"),
            "workflow_name": workflow.get("name"),
        }

    async def _execute_group(
        self,
        workflow: dict,
        action: str,
        step: dict,
        results: list[dict[str, Any]],
        dry_run: bool,
    ) -> dict[str, Any]:
        """Run a `parallel` (explicit sub-steps) or `map` (one template over a
        list) fan-out. Sub-steps run concurrently under a bounded semaphore and
        see the SAME top-level `results` for {{steps.N}} interpolation. The group
        result exposes `results` (each sub-step's result value, positional) plus
        `records` (full sub-step records with status)."""
        params = step.get("params", {})
        # substeps: list of (substep_dict, scope_or_None)
        if action == "parallel":
            substeps = [(s, None) for s in (params.get("steps") or [])]
        else:  # map
            over_raw = params.get("over")
            over = (
                self._render_params(over_raw, results, self._ctx(workflow))
                if isinstance(over_raw, str)
                else over_raw
            )
            items = self._coerce_list(over)
            template = params.get("template") or {}
            substeps = [
                (template, {"item": item, "index": i}) for i, item in enumerate(items)
            ]

        if not substeps:
            return {"results": [], "records": [], "count": 0, "failed": 0}

        max_conc = int(params.get("max_concurrent") or 0) or len(substeps)
        sem = asyncio.Semaphore(max(1, max_conc))

        async def run_one(i: int, sub: dict, sc: dict | None) -> dict:
            async with sem:
                return await self._execute_step(
                    workflow=workflow,
                    index=i,
                    step=sub,
                    results=results,
                    dry_run=dry_run,
                    scope=sc,
                )

        subrecords = await asyncio.gather(
            *[run_one(i, s, sc) for i, (s, sc) in enumerate(substeps)]
        )
        failed = sum(1 for r in subrecords if r.get("status") == "failed")
        return {
            "results": [r.get("result") for r in subrecords],
            "records": subrecords,
            "count": len(subrecords),
            "failed": failed,
        }

    async def _perform_action(
        self,
        workflow: dict,
        action: str,
        params: dict[str, Any],
        results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        results = results or []
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
            out = {
                "session_id": session["_id"],
                "workspace": session["workspace"],
                "backend": session["backend"],
                "status": session.get("status"),
            }
            # await:true blocks until the session finishes and captures its
            # result summary, so a fan-out of code_sessions can be synthesized.
            if params.get("await"):
                timeout = params.get("timeout")
                final = await self.coding_manager.wait_for_session(
                    session["_id"], timeout=float(timeout) if timeout else None
                )
                if final:
                    out["status"] = final.get("status")
                    out["result_summary"] = final.get("result_summary")
                    out["timed_out"] = bool(final.get("timed_out", False))
            return out
        if action == "synthesize":
            return await self._run_synthesize(workflow, params, results)
        if action == "prompt":
            return await self._run_prompt_action(workflow, params)
        raise ValueError(f"Unsupported workflow action: {action}")

    async def _run_synthesize(
        self, workflow: dict, params: dict[str, Any], results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Reduce prior results into a single answer with one agent turn — the
        synthesis stage of a fan-out (multi-model review / parallel research →
        one merged result). `inputs` is a list of (already-interpolated) strings;
        `from_steps` is a convenience that pulls whole step results by index."""
        inputs = params.get("inputs")
        if inputs is None and params.get("from_steps") is not None:
            inputs = []
            for idx in params["from_steps"]:
                res = results[idx].get("result") if 0 <= idx < len(results) else None
                inputs.append(self._stringify(res))
        inputs = inputs or []
        instruction = params.get("instruction") or (
            "Synthesize the following results into a single coherent answer."
        )
        parts = [f"### Input {i + 1}\n{self._stringify(x)}" for i, x in enumerate(inputs)]
        message = instruction + "\n\n" + "\n\n".join(parts)
        return await self._run_prompt_action(
            workflow,
            {
                "message": message,
                "title": params.get("title"),
                "backend": params.get("backend"),
                "model": params.get("model"),
            },
        )

    async def _run_prompt_action(self, workflow: dict, params: dict[str, Any]) -> dict[str, Any]:
        orchestrator = Orchestrator(self.db, self.tool_router, task_runner=self.task_runner, coding_manager=self.coding_manager)
        agent = await self.db.agents.find_one({"is_default": True})
        if not agent:
            agent = await self.db.agents.find_one({}, sort=[("created_at", 1)])
        if not agent:
            raise RuntimeError("No agent available for workflow prompt action")
        now = datetime.now(timezone.utc)
        # Optional per-action backend/model override (e.g. synthesize on Opus).
        # When set, pin it fallback-free via llm_config_override so the merge runs
        # on the model the caller chose rather than the default agent's.
        backend = params.get("backend") or agent["llm"]["backend"]
        model = params.get("model") or agent["llm"]["model"]
        convo = {
            "agent_id": agent["_id"],
            "active_agent_id": None,
            "title": params.get("title") or f"Workflow {workflow['name']}",
            "summary": None,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "llm_config": {
                "backend": backend,
                "model": model,
                "temperature": agent["llm"]["temperature"],
            },
            "messages": [],
            "tags": ["workflow"],
            "pinned": False,
            "stats": {"message_count": 0, "total_tokens": 0, "tool_calls": 0},
        }
        if params.get("backend") or params.get("model"):
            convo["llm_config_override"] = {"backend": backend, "model": model}
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

    def _render_params(
        self,
        value: Any,
        results: list[dict[str, Any]],
        context: dict[str, Any],
        scope: dict[str, Any] | None = None,
    ) -> Any:
        if isinstance(value, dict):
            return {key: self._render_params(inner, results, context, scope) for key, inner in value.items()}
        if isinstance(value, list):
            return [self._render_params(item, results, context, scope) for item in value]
        if isinstance(value, str):
            rendered = re.sub(
                r"\{\{steps\.(\d+)(?:\.([a-zA-Z0-9_.-]+))?\}\}",
                lambda match: str(self._lookup_result(results, int(match.group(1)), match.group(2)) or ""),
                value,
            )
            rendered = re.sub(
                r"\{\{workflow\.([a-zA-Z0-9_.-]+)\}\}",
                lambda match: str(context.get(match.group(1), "")),
                rendered,
            )
            # Per-item scope inside a `map` fan-out: {{item}}, {{item.path}}, {{index}}.
            if scope:
                rendered = re.sub(
                    r"\{\{index\}\}",
                    lambda _m: str(scope.get("index", "")),
                    rendered,
                )
                rendered = re.sub(
                    r"\{\{item(?:\.([a-zA-Z0-9_.-]+))?\}\}",
                    lambda match: str(
                        self._lookup_scope(scope.get("item"), match.group(1)) or ""
                    ),
                    rendered,
                )
            return rendered
        return value

    def _lookup_result(self, results: list[dict[str, Any]], index: int, path: str | None) -> Any:
        if index < 0 or index >= len(results):
            return ""
        result = results[index].get("result")
        return self._walk(result, path)

    @staticmethod
    def _walk(current: Any, path: str | None) -> Any:
        """Walk a dotted path over nested dicts AND lists (numeric parts index a
        list), so {{steps.2.results.0.result_summary}} resolves into a fan-out."""
        if not path:
            return current
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return current

    def _lookup_scope(self, item: Any, path: str | None) -> Any:
        return self._walk(item, path)

    @staticmethod
    def _coerce_list(value: Any) -> list:
        """Normalize a `map` `over` value to a list. Accepts an actual list, a
        JSON-array string, or a newline/comma-separated string (interpolation
        renders results to strings)."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return []
            try:
                import json as _json
                parsed = _json.loads(s)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
            if "\n" in s:
                return [ln.strip() for ln in s.splitlines() if ln.strip()]
            return [x.strip() for x in s.split(",") if x.strip()]
        return [value]

    @staticmethod
    def _stringify(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            import json as _json
            return _json.dumps(value, default=str, indent=2)
        except Exception:
            return str(value)
