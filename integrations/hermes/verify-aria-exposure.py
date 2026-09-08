#!/usr/bin/env python3
"""Exercise installed Hermes MCP registration/dispatch with read-only ARIA calls.

Run with the installed Hermes Python. Optional --staged-source substitutes only
the bridge process in this probe, never persisted configuration. No agent/model
turn, gateway restart, messages, controller changes or credentials in evidence.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import sys
import time

HERMES = Path("/Users/ben/Services/apps/hermes-agent")
PROFILE = Path("/Users/ben/Services/data/hermes-home")
DEPLOYED = Path("/Users/ben/Services/apps/aria-mcp/server.py")
BRIDGE_PYTHON = "/Users/ben/Services/apps/aria-mcp/.venv/bin/python"
ADDITIONS = {"operator_snapshot", "inference_backend", "inference_usage",
             "inference_traces", "benchmark_status", "ralph_status", "get_task"}


def unpack(raw):
    value = json.loads(raw)
    if "error" in value:
        # Never persist the error body; it may contain private upstream data.
        raise ValueError("Hermes tool dispatch failed")
    if "structuredContent" in value:
        result = value["structuredContent"]
        return result.get("result", result)
    result = value.get("result", value)
    return json.loads(result) if isinstance(result, str) else result


def probe(args):
    if platform.system() != "Darwin":
        raise ValueError("Use the Mac control plane")
    os.environ["HERMES_HOME"] = str(PROFILE)
    sys.path.insert(0, str(HERMES))
    from dotenv import dotenv_values
    import yaml
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    from tools import mcp_tool
    from tools.registry import registry

    config = yaml.safe_load((PROFILE / "config.yaml").read_text())
    token = set_secret_scope(dotenv_values(PROFILE / ".env"))
    try:
        transport = mcp_tool._interpolate_env_vars(config["mcp_servers"]["aria"])
    finally:
        reset_secret_scope(token)
    if (transport.get("enabled") is not True
            or transport.get("command") != "/Users/ben/Services/apps/bin/run-aria-mcp"
            or transport.get("env", {}).get("ARIA_API_URL") != "http://127.0.0.1:8200"):
        raise ValueError("Unexpected configured MCP transport")
    key = transport.get("env", {}).get("ARIA_API_KEY", "")
    expected_key = dotenv_values("/Users/ben/Services/apps/ProjectAria/.env").get("API_KEY", "")
    if not key or not expected_key or not hmac.compare_digest(key, expected_key):
        raise ValueError("Configured profile credential mismatch")
    source = args.staged_source.resolve() if args.staged_source else DEPLOYED
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    if source_sha != args.expected_sha256:
        raise ValueError("Bridge differs from reviewed source")
    dependency = source.with_name("operations.py")
    dependency_sha = hashlib.sha256(dependency.read_bytes()).hexdigest() if dependency.is_file() else None
    if dependency_sha != args.expected_operations_sha256:
        raise ValueError("Operations dependency differs from supplied review pin")
    if args.staged_source:
        transport = {**transport, "command": BRIDGE_PYTHON, "args": [str(source)]}
    started = time.monotonic()
    calls = []

    def call(name, arguments):
        at = time.monotonic()
        print(json.dumps({"checking_tool": name}), flush=True)
        result = unpack(registry.dispatch(mcp_tool.mcp_prefixed_tool_name("aria", name), arguments))
        calls.append({"tool": name, "passed": True, "seconds": round(time.monotonic() - at, 3)})
        return result

    try:
        registered = set(mcp_tool.register_mcp_servers({"aria": transport}))
        wanted = {mcp_tool.mcp_prefixed_tool_name("aria", n) for n in ADDITIONS}
        if not wanted <= registered:
            raise ValueError("Hermes registration missing new tools")
        # Actual installed Hermes schema conversion/filtering, not a hand-made
        # OpenAI tools array. MCP's read-only annotations are also verified.
        definitions = registry.get_definitions(registered, quiet=True)
        if not wanted <= {d["function"]["name"] for d in definitions}:
            raise ValueError("New tools not available in installed Hermes registry")
        from hermes_cli.tools_config import _get_platform_tools
        from toolsets import resolve_toolset
        from tools.tool_search import dispatch_tool_search, dispatch_tool_describe, ToolSearchConfig
        platform_groups = sorted(_get_platform_tools(config, "signal"))
        selected_names = set()
        for group in platform_groups:
            selected_names.update(resolve_toolset(group))
        if not wanted <= selected_names:
            raise ValueError("Signal platform selection excludes new tools")
        # Native progressive discovery, preserving deferred full schemas.
        search_config = ToolSearchConfig.from_raw((config.get("tools") or {}).get("tool_search"))
        for name in wanted:
            hits = json.loads(dispatch_tool_search({"query": name, "limit": 5},
                                                 current_tool_defs=definitions, config=search_config))
            if name not in {row["name"] for row in hits["matches"]}:
                raise ValueError("Native tool search cannot find addition")
            described = json.loads(dispatch_tool_describe({"name": name}, current_tool_defs=definitions))
            if described.get("name") != name or "parameters" not in described:
                raise ValueError("Native tool describe failed")
        server = mcp_tool._servers["aria"]
        for tool in server._tools:
            if tool.name in ADDITIONS:
                if not tool.annotations or tool.annotations.readOnlyHint is not True:
                    raise ValueError("Missing read-only annotation")
        contract = call("tool_contract_status", {})
        if contract.get("sha256") != source_sha or contract.get("version") != args.expected_version:
            raise ValueError("Wrong running bridge contract")
        if dependency_sha and contract.get("operations_sha256") != dependency_sha:
            raise ValueError("Wrong loaded operations dependency")
        snapshot = call("operator_snapshot", {"model": args.model})
        if not snapshot.get("complete"):
            raise ValueError("Incomplete live operator snapshot")
        backend = call("inference_backend", {"model": args.model})
        if backend.get("backend") != args.model:
            raise ValueError("Explicit model route mismatch")
        for group in ("summary", "caller", "model"):
            call("inference_usage", {"group_by": group, "days": 1})
        traces = call("inference_traces", {"hours": 1, "limit": 3})["traces"]
        if not isinstance(traces, list) or len(traces) > 3:
            raise ValueError("Trace result shape is not bounded")
        for row in traces:
            if {"prompt", "messages", "content", "completion", "api_key"} & set(row):
                raise ValueError("Trace includes content or credentials")
        benchmarks = call("benchmark_status", {"limit": 2})
        ralph = call("ralph_status", {"limit": 2})
        task = call("get_task", {"id": args.task_id})
        if task.get("id") != args.task_id:
            raise ValueError("Planning task read mismatch")
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha:
            raise ValueError("Bridge changed during test")
        if dependency_sha and hashlib.sha256(dependency.read_bytes()).hexdigest() != dependency_sha:
            raise ValueError("Operations dependency changed during test")
        return {"passed": True, "configured_transport": not bool(args.staged_source),
                "installed_hermes_registry": True, "profile_key_matches": True,
                "contract": contract, "new_tools": sorted(ADDITIONS),
                "registered_tool_count": len(registered), "bridge_tool_count": len(server._tools),
                "schema_characters": len(json.dumps(definitions)), "model": backend.get("backend"),
                "operations_file_sha256": dependency_sha,
                "signal_platform_selected": True, "native_search_describe_passed": True,
                "benchmark_harness_available": benchmarks.get("available"),
                "ralph_rows": ralph.get("returned"), "trace_rows": len(traces),
                "calls": calls, "wall_seconds": round(time.monotonic() - started, 3),
                "scope": "Fresh installed Hermes MCP registry/dispatch; no existing Signal process reload or LLM turn"}
    finally:
        mcp_tool.shutdown_mcp_servers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged-source", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-version", default="2026-09-08.2")
    parser.add_argument("--expected-operations-sha256")
    parser.add_argument("--model", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Use a new evidence path")
    try:
        result = probe(args)
    except Exception as exc:
        result = {"passed": False, "error_type": type(exc).__name__}
    result["instrument_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with args.out.open("x") as out:
        json.dump(result, out, indent=2)
        out.write("\n")
    print(json.dumps(result), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
