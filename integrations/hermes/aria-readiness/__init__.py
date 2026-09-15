"""Aria-owned Hermes diagnostics, using plugin hooks without changing core.

No network requests on the healthy turn path. Missing transports are handed to
Hermes's own MCP connector in a bounded background retry. Hermes owns tool
snapshot refresh at turn boundaries; this plugin never mutates an agent prompt.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
import time

LOG = logging.getLogger("aria.readiness")
PREFIX = "mcp__aria__"
REQUIRED = {PREFIX + name for name in ("fleet_status", "create_coding_session", "list_nodes",
                                      "resume_coding_session", "loop_policy", "benchmark_catalog",
                                      "wait_for_shell_output", "host_temperatures", "get_memory",
                                      "store_memory", "awareness_observations", "get_research_report",
                                      "red_model_status", "select_red_model")}
_lock = threading.RLock()
_recovery_running = False
_last_recovery = 0.0
_turn_states = {}
_last_report = {}


def snapshot(platform="signal"):
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import resolve_toolset
    from tools.mcp_tool_discovery import get_mcp_status
    from tools.registry import registry
    cfg = load_config()
    connection = next((r for r in get_mcp_status() if r["name"] == "aria"), {})
    registered = {n for n in registry.get_all_tool_names() if n.startswith(PREFIX)}
    selected = set()
    for group in _get_platform_tools(cfg, platform or "signal"):
        selected.update(resolve_toolset(group))
    selected &= registered
    return {
        "pid": os.getpid(), "platform": platform or "signal", "updated_at": time.time(),
        "configured": "aria" in cfg.get("mcp_servers", {}),
        "connection": connection.get("status", "unconfigured"),
        "connected": bool(connection.get("connected")),
        "registered_count": len(registered), "selected_count": len(selected),
        "missing_required": sorted(REQUIRED - selected),
        "ready": bool(connection.get("connected")) and REQUIRED <= selected,
    }


def record(report):
    from hermes_constants import get_hermes_home
    global _last_report
    with _lock:
        # Preserve the model-visible observation until another request replaces it.
        previous = _last_report
        if previous.get("pid") == os.getpid():
            for key in ("model_visible", "model_visible_at", "session_id", "model"):
                if key in previous and key not in report:
                    report[key] = previous[key]
        _last_report = dict(report)
        home = Path(get_hermes_home())
        directory = home / "aria-readiness"
        directory.mkdir(exist_ok=True)
        target = directory / f"{os.getpid()}.json"
        temporary = target.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(target)
        try:
            gateway_pid = json.loads((home / "gateway_state.json").read_text()).get("pid")
        except (OSError, ValueError):
            gateway_pid = None
        # A CLI/probe sharing this home must never overwrite gateway evidence.
        if gateway_pid == os.getpid():
            alias = home / "aria-tools-ready.json"
            temporary = alias.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(report, indent=2) + "\n")
            temporary.chmod(0o600)
            temporary.replace(alias)


def recover():
    global _recovery_running, _last_recovery
    with _lock:
        if _recovery_running or time.monotonic() - _last_recovery < 60:
            return
        _recovery_running = True
        _last_recovery = time.monotonic()

    def work():
        global _recovery_running
        try:
            from tools.mcp_tool_discovery import discover_mcp_tools
            from tools.mcp_tool_loop import reconnect_mcp_server
            if not reconnect_mcp_server("aria"):
                discover_mcp_tools(allowed_mcp_names=["aria"])
            LOG.info("Requested Aria MCP recovery through Hermes connector")
            # Discovery/reconnect can finish after the startup observer exits.
            # Refresh the receipt rather than leaving a stale "not ready" flag
            # until the next personal conversation. Never alter agent history.
            for _ in range(15):
                report = snapshot()
                record(report)
                if report["ready"]:
                    break
                time.sleep(1)
        except Exception as exc:
            LOG.warning("Aria MCP recovery failed (%s)", type(exc).__name__)
        finally:
            with _lock:
                _recovery_running = False

    threading.Thread(target=work, name="aria-mcp-recovery", daemon=True).start()


def before_turn(session_id="", platform="signal", **_):
    report = snapshot(platform)
    record(report)
    if report["configured"] and not report["connected"] and report["connection"] != "disabled":
        recover()
    state = (report["ready"], report["connection"], tuple(report["missing_required"]))
    with _lock:
        if _turn_states.get(session_id) == state:
            return None
        if len(_turn_states) >= 256:
            _turn_states.pop(next(iter(_turn_states)))
        _turn_states[session_id] = state
    if report["ready"]:
        return {"context": "Aria MCP is connected and its tools are selected. When full schemas are deferred, "
                "use tool_search to find Aria capabilities, tool_describe for parameters, and tool_call to execute. "
                "For Red Linux, prefer red_model_status and select_red_model (qwen3.8-27b-paro-int5 is the Red default; qwen3.8-27b, qwen-flash-next and qwen-paro are alternatives) "
                "for wake/load/switch. Only status=ready confirms a load; on pending/error read status once and report unresolved state. Do not automatically retry or use terminal/SSH/WoL fallbacks. "
                "These tools do not change Hermes's own model or default routing. "
                "Do not infer that Aria is missing merely because individual tools are not listed directly."}
    return {"context": "Aria tool readiness check reports " + report["connection"] +
            ". Required tools missing from the selected catalog: " + ", ".join(report["missing_required"]) +
            ". A missing connection has been handed to Hermes's reconnect path when enabled. "
            "Do not claim fleet state or launch unmanaged work. If tools remain absent, /reload-mcp refreshes this chat."}


def before_request(request=None, session_id="", platform="signal", model="", **_):
    body = (request or {}).get("body", {})
    tools = body.get("tools")
    report = snapshot(platform)
    visible = None
    if isinstance(tools, list):
        names = {t.get("function", t).get("name", "") for t in tools if isinstance(t, dict)}
        visible = {"direct_aria_count": sum(n.startswith(PREFIX) for n in names),
                   "tool_search": "tool_search" in names,
                   "tool_describe": "tool_describe" in names,
                   "tool_call": "tool_call" in names}
        visible["aria_accessible"] = report["ready"] and (
            REQUIRED <= names or all(visible[n] for n in ("tool_search", "tool_describe", "tool_call")))
    report.update(model_visible=visible, model_visible_at=time.time(), session_id=session_id, model=model)
    record(report)
    if not report["ready"] or visible and not visible["aria_accessible"]:
        LOG.warning("Aria tools unavailable to request: connection=%s selected=%s visible=%s",
                    report["connection"], report["selected_count"], visible)


def register(ctx):
    ctx.register_hook("pre_llm_call", before_turn)
    ctx.register_hook("pre_api_request", before_request)

    def startup():
        # Startup discovery belongs to Hermes. Observe it without racing it.
        for _ in range(30):
            time.sleep(2)
            try:
                report = snapshot()
                record(report)
                if report["ready"]:
                    return
            except Exception as exc:
                LOG.debug("Aria startup observation pending (%s)", type(exc).__name__)
        recover()

    threading.Thread(target=startup, name="aria-readiness", daemon=True).start()
