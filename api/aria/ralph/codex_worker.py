"""Codex reasoning with controller-executed tools, never a host coding shell.

One app-server process/thread per attempt; subsequent bounded turns use that
thread. Native environment access is disabled. Repository commands travel only
through Ralph's existing executor, which owns containment and cancellation.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path

from pydantic import Field, model_validator

from aria.core.logging import scrub_secrets
from aria.ralph.models import Plan, StrictModel, WorkerReport, safe_relative
from aria.ralph.runtime import process
from aria.ralph.worker import ContextLimit, ModelConfigurationError, ModelExecutionError


class Step(StrictModel):
    action: str = Field(pattern="^(shell|list_files|read_file|report|plan)$")
    argument: str = Field(max_length=12000)
    report: WorkerReport | None
    plan: Plan | None

    @model_validator(mode="after")
    def exclusive(self):
        if ((self.action == "report") != (self.report is not None)
                or (self.action == "plan") != (self.plan is not None)
                or self.action in {"report", "plan", "list_files"} and self.argument):
            raise ValueError("Step must contain exactly one action")
        return self


def step_schema():
    schema = Step.model_json_schema()

    def strict(value):
        if isinstance(value, dict):
            if "properties" in value:
                value["required"] = list(value["properties"])
            value.pop("default", None)
            for child in value.values():
                strict(child)
        elif isinstance(value, list):
            for child in value:
                strict(child)
    strict(schema)
    return schema


def tool_context(result, remaining_turns):
    output = str(result.get("output", ""))
    return json.dumps({
        "tool_result": {**result, "output": output[:6000], "output_truncated": len(output) > 6000},
        "remaining_controller_turns": remaining_turns,
        "instruction": "Use only relevant missing sections if output was truncated. Reserve a turn for your report.",
    })


class CodexConnection:
    """Private stdio JSON-RPC connection; no daemon, socket, or resumed thread."""

    def __init__(self, binary, directory):
        self.binary, self.directory = binary, directory
        self.proc = None
        self.counter = 0
        self.stderr_task = None
        self.total_output = 0

    async def __aenter__(self):
        # Deliberately pin the environment-disable protocol tested by this
        # integration. A CLI upgrade needs a fresh containment qualification.
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG", "CODEX_HOME"}}
        code, output = await process([self.binary, "--version"], env=env)
        if code or output.strip() != b"codex-cli 0.153.2":
            raise ModelConfigurationError("Ralph Codex worker requires qualified codex-cli 0.153.2")
        self.proc = await asyncio.create_subprocess_exec(
            self.binary, "app-server", "--listen", "stdio://",
            cwd=self.directory, env=env, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=1024 * 1024,
        )

        async def drain():
            # Startup/provider diagnostics can contain configuration; don't
            # send them to a worker or retain raw credential-bearing output.
            while await self.proc.stderr.read(8192):
                pass
        self.stderr_task = asyncio.create_task(drain())
        try:
            await self.rpc("initialize", {"clientInfo": {"name": "aria_ralph", "version": "1"},
                                           "capabilities": {"experimentalApi": True}})
            await self.send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        if self.proc:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.proc.wait()
        if self.stderr_task:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)

    async def send(self, value):
        self.proc.stdin.write(json.dumps(value).encode() + b"\n")
        await self.proc.stdin.drain()

    async def read(self):
        line = await self.proc.stdout.readline()
        self.total_output += len(line)
        if not line or self.total_output > 8 * 1024 * 1024:
            raise ModelExecutionError("Codex exited or exceeded the bounded event log")
        value = json.loads(line)
        if "method" in value and "id" in value:
            # No approval, MCP, native/dynamic tool or input request is ever
            # accepted. This worker returns proposed commands as data only.
            await self.send({"id": value["id"], "error": {"code": -32601, "message": "Unavailable in Ralph"}})
            raise ModelExecutionError("Codex requested a capability outside the Ralph worker contract")
        if value.get("method") in {"item/started", "item/completed"}:
            kind = value.get("params", {}).get("item", {}).get("type")
            if kind not in {"userMessage", "agentMessage", "reasoning"}:
                raise ModelExecutionError(f"Unexpected Codex item: {kind}; native tools are disabled")
        return value

    async def rpc(self, method, params):
        self.counter += 1
        identifier = self.counter
        await self.send({"id": identifier, "method": method, "params": params})
        while True:
            message = await self.read()
            if message.get("id") == identifier:
                if "error" in message:
                    raise ModelConfigurationError(scrub_secrets(str(message["error"]))[:1000])
                return message["result"]

    async def start(self, model, contract, reasoning_effort=None):
        current = await self.rpc("config/read", {"includeLayers": False})
        config = current.get("config", {})
        overrides = {
            "web_search": "disabled", "project_doc_max_bytes": 0,
        }
        if reasoning_effort:
            overrides["model_reasoning_effort"] = reasoning_effort
        # Disable configured MCP servers individually: an empty table can be
        # merged with inherited configuration and leave servers enabled.
        for name in config.get("mcp_servers", {}):
            overrides[f"mcp_servers.{name}.enabled"] = False
        for feature in (
            "shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent",
            "multi_agent_v2", "computer_use", "browser_use", "browser_use_external",
            "image_generation", "view_image", "memories", "goals", "sleep_tool",
            "skill_search", "workspace_dependencies", "shell_snapshot",
            "code_mode", "code_mode_host", "code_mode_only", "request_permissions_tool",
        ):
            overrides[f"features.{feature}"] = False
        result = await self.rpc("thread/start", {
            "model": model, "cwd": self.directory, "ephemeral": True,
            "approvalPolicy": "never", "sandbox": "read-only", "environments": [],
            "baseInstructions": contract, "config": overrides,
        })
        return result["thread"]["id"]

    async def turn(self, thread, text):
        await self.rpc("turn/start", {
            "threadId": thread, "input": [{"type": "text", "text": text}],
            "environments": [], "outputSchema": step_schema(),
        })
        content, usage = None, None
        while True:
            event = await self.read()
            method, params = event.get("method"), event.get("params", {})
            if params.get("threadId") != thread:
                continue
            if method == "item/completed" and params["item"]["type"] == "agentMessage":
                content = params["item"]["text"]
            elif method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage", {}).get("total", {}).get("totalTokens")
            elif method == "turn/completed":
                if params["turn"]["status"] != "completed" or content is None:
                    detail = scrub_secrets(str(params["turn"].get("error") or "missing structured proposal"))[:1000]
                    raise ModelExecutionError("Codex turn failed: " + detail)
                return content, usage


class CodexWorker:
    def __init__(self, binary=None, connection_factory=CodexConnection):
        if binary is None:
            from aria.config import settings
            binary = settings.codex_binary
        self.binary, self.connection_factory = binary, connection_factory

    async def run(self, *, session_id, task, specification, instructions, handoff,
                  config, limits, execute, meter, log, planning=False, check_ids=None):
        contract = Path(__file__).with_name("worker_prompt.txt").read_text()
        contract += (
            "\nNative environment access and native tools are disabled. Propose one action as JSON matching "
            "the supplied output schema. Ralph executes permitted repository tools in /workspace and returns "
            "their output on the next turn of this same conversation. Do not use native tools. "
            "For shell/read_file put the command/relative path in argument; otherwise argument is empty. "
            "Only report actions have a report; only plan actions have a plan; all other fields are null."
        )
        if planning:
            contract = (
                "You are inspecting an existing repository and proposing small dependency-aware tasks. "
                "Do not implement changes. Only list_files and read_file actions are available, then plan. "
                "Use only approved check IDs; expose any unmet automatic criteria as human_review.\n"
                + contract[contract.index("\nNative environment"):]
            )
        else:
            contract += " Available actions are shell and report. Implement only the assigned task."
        contract += (
            f" You have at most {limits['turns']} controller turns, including the final report. "
            "The supplied specification and operating instructions are already in context. Read the compact "
            "handoff and relevant implementation sections; do not reread the entire delivery history or logs. "
            "Tool output is bounded: keep inspections focused and batch small related commands. "
            "If blocked, use the final turn to report durable findings and exact next steps."
        )
        context = {"session_id": session_id, "task": task, "specification": specification,
                   "operating_instructions": instructions, "previous_handoff": handoff,
                   "approved_check_ids": check_ids if planning else None,
                   "capabilities": {"workspace": "/workspace", "network": False, "read_only": planning}}
        text = json.dumps(context)
        chars, previous_tokens, inspected = len(contract) + len(text), 0, False
        with tempfile.TemporaryDirectory(prefix="aria-ralph-reasoner-") as directory:
            async with self.connection_factory(self.binary, directory) as connection:
                thread = await connection.start(config["model"], contract, config.get("reasoning_effort"))
                await log("provider_session", {"session_id": session_id, "provider_session_id": thread,
                                               "backend": "codex", "fresh": True})
                for turn_index in range(limits["turns"]):
                    if chars > limits["context_chars"]:
                        raise ContextLimit("Codex attempt context limit exceeded")
                    await meter("reserve", None)
                    content, total = await connection.turn(thread, text)
                    usage = None
                    if type(total) is int and total > previous_tokens:
                        usage = {"total_tokens": total - previous_tokens}
                        previous_tokens = total
                    await meter("usage", usage)
                    chars += len(content)
                    step = Step.model_validate_json(content)
                    await log("model", {"session_id": session_id, "provider_session_id": thread,
                                        "content": content, "usage": usage})
                    if step.action == "report" and not planning:
                        return step.report.model_dump()
                    if step.action == "plan" and planning and inspected:
                        return step.plan.model_dump()
                    if step.action == "shell" and not planning and step.argument.strip():
                        argv = ["/bin/sh", "-c", step.argument]
                    elif step.action == "list_files" and planning:
                        argv = ["/usr/bin/find", "/workspace", "-type", "f"]
                    elif step.action == "read_file" and planning:
                        argv = ["/bin/cat", "--", "/workspace/" + safe_relative(step.argument)]
                    else:
                        raise ValueError("Unavailable action or planning attempted without code inspection")
                    result = await execute(argv)
                    inspected = inspected or step.action == "read_file" and result["exit_code"] == 0
                    await log("planning_inspection" if planning else "tool", {
                        "session_id": session_id, "argv": argv, **result,
                    })
                    text = tool_context(result, limits["turns"] - turn_index - 1)
                    chars += len(text)
        raise ContextLimit("Codex controller turn limit exhausted")
