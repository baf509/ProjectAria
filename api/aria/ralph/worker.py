"""A small tool-enabled harness over Aria's existing provider abstraction."""
from __future__ import annotations

import json
import re
from pathlib import Path

from aria.llm.base import Message, Tool
from aria.llm.manager import llm_manager
from aria.ralph.models import Plan, WorkerReport, safe_relative


SHELL = Tool("shell", "Inspect, edit, and test in /workspace. No network or controller access.", {
    "type": "object", "properties": {"command": {"type": "string", "maxLength": 12000}},
    "required": ["command"], "additionalProperties": False,
})
LIST_FILES = Tool("list_files", "List files in the read-only repository.", {
    "type": "object", "properties": {}, "additionalProperties": False,
})
READ_FILE = Tool("read_file", "Read an existing repository file to inform the proposed plan.", {
    "type": "object", "properties": {"path": {"type": "string", "maxLength": 1000}},
    "required": ["path"], "additionalProperties": False,
})


class ContextLimit(RuntimeError):
    pass


class ModelExecutionError(RuntimeError):
    pass


class ModelConfigurationError(RuntimeError):
    pass


def structured_reply(content: str) -> str:
    # Existing local adapters can prefix content with a tagged reasoning block.
    # Remove only an outer wrapper; arbitrary prose or multiple JSON objects
    # still fail the strict Plan/WorkerReport schema below.
    text = re.sub(r"^\s*<think>.*?</think>\s*", "", content, count=1, flags=re.S | re.I).strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    return text


class AgentWorker:
    """One new messages list per attempt; normal multi-call context within it.

    Adapter instances are stateless transports, not conversations. The durable
    session ID identifies each logical conversation in all logs and prompts.
    """

    def __init__(self, adapter_factory=None):
        self.adapter_factory = adapter_factory or llm_manager.get_adapter

    async def run(self, *, session_id, task, specification, instructions, handoff,
                  config, limits, execute, meter, log, planning=False, check_ids=None):
        if config["backend"] == "codex":
            from aria.ralph.codex_worker import CodexWorker
            return await CodexWorker().run(
                session_id=session_id, task=task, specification=specification,
                instructions=instructions, handoff=handoff, config=config, limits=limits,
                execute=execute, meter=meter, log=log, planning=planning, check_ids=check_ids,
            )
        if planning:
            contract = (
                "You are proposing a plan, not implementing changes. Inspect actual repository code using read_file "
                "before proposing tasks. The repository is read-only. Compare the supplied specification to "
                "the existing implementation. Propose small dependency-aware tasks with explicit acceptance "
                "criteria, context references, permitted paths and out-of-scope boundaries. Only use the "
                "supplied approved check IDs. If those checks cannot establish acceptance, populate human_review. "
                "Return only JSON matching this schema: " + json.dumps(Plan.model_json_schema())
            )
        else:
            contract = Path(__file__).with_name("worker_prompt.txt").read_text()
        context = {"session_id": session_id, "task": task, "specification": specification,
                   "operating_instructions": instructions, "previous_handoff": handoff,
                   "approved_check_ids": check_ids if planning else None,
                   "capabilities": {"workspace": "/workspace", "network": False, "read_only": planning}}
        messages = [Message("system", contract), Message("user", json.dumps(context))]
        try:
            adapter = self.adapter_factory(config["backend"], config["model"])
        except (ValueError, ImportError) as exc:
            raise ModelConfigurationError(str(exc)) from exc
        inspected = False
        for _ in range(limits["turns"]):
            size = sum(len(m.content or "") + len(json.dumps(m.tool_calls or [])) for m in messages)
            if size > limits["context_chars"]:
                raise ContextLimit("Attempt context limit reached; use compact handoff on next attempt")
            await meter("reserve", None)
            try:
                content, calls, usage = await adapter.complete(
                    messages, tools=[LIST_FILES, READ_FILE] if planning else [SHELL], temperature=0.2, max_tokens=4096,
                )
            except Exception as exc:
                raise ModelExecutionError(f"Provider {config['backend']} failed: {exc}") from exc
            await meter("usage", usage)
            await log("model", {"session_id": session_id, "content": content,
                                "calls": [vars(c) for c in calls], "usage": usage})
            if len(calls) > 8:
                raise ValueError("Worker exceeded 8 tool calls per model turn")
            # Message carries Aria's provider-neutral ToolCall dictionaries;
            # each adapter constructs its own wire representation.
            messages.append(Message("assistant", content or "", tool_calls=[
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls
            ] or None))
            if not calls:
                if planning and not inspected:
                    raise ValueError("Planner must inspect repository before proposing a plan")
                model = Plan if planning else WorkerReport
                return model.model_validate_json(structured_reply(content)).model_dump()
            for call in calls:
                if planning:
                    if call.name == "list_files" and call.arguments == {}:
                        result = await execute(["/usr/bin/find", "/workspace", "-type", "f"])
                    elif call.name == "read_file" and isinstance(call.arguments, dict) and set(call.arguments) == {"path"}:
                        path = call.arguments["path"]
                        if not isinstance(path, str) or len(path) > 1000:
                            raise ValueError("Invalid planner file path")
                        result = await execute(["/bin/cat", "--", "/workspace/" + safe_relative(path)])
                        inspected = inspected or result["exit_code"] == 0
                    else:
                        raise ValueError("Planner has read_file and list_files tools only")
                    await log("planning_inspection", {"session_id": session_id, "tool": call.name, "arguments": call.arguments, **result})
                    messages.append(Message("tool", json.dumps(result)[:8000], tool_call_id=call.id, name=call.name))
                    continue
                if (call.name != "shell" or not isinstance(call.arguments, dict)
                        or set(call.arguments) != {"command"}
                        or not isinstance(call.arguments["command"], str)
                        or not 0 < len(call.arguments["command"]) <= 12000):
                    raise ValueError("Worker attempted an unavailable tool or invalid arguments")
                result = await execute(["/bin/sh", "-c", call.arguments["command"]])
                inspected = inspected or result["exit_code"] == 0
                await log("tool", {"session_id": session_id, "command": call.arguments["command"], **result})
                messages.append(Message("tool", json.dumps(result)[:8000], tool_call_id=call.id, name="shell"))
        raise ContextLimit("Agent turn budget exhausted")
