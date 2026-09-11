"""Exercise actual MCP schemas and operation behavior without production writes."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP


@pytest.fixture
def operations(tmp_path, monkeypatch):
    path = Path(__file__).parents[2] / "mcp/operations.py"
    spec = importlib.util.spec_from_file_location("aria_operations_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    key_file = tmp_path / "admin-key"
    key_file.write_text("test-service-secret")
    monkeypatch.setenv("ARIA_ADMIN_KEY_FILE", str(key_file))
    server = FastMCP("test")
    request = AsyncMock(return_value={"id": "test-run", "state": "draft", "version": 2})
    module.register(server, request)
    return module, server, request


@pytest.mark.asyncio
async def test_real_mcp_schema_excludes_context_and_credential(operations):
    _, server, _ = operations
    tools = {tool.name: tool for tool in await server.list_tools()}
    schema = tools["control_loop_run"].inputSchema
    assert "ctx" not in schema["properties"]
    assert set(schema["properties"]["action"]["enum"]) == {"plan", "start", "pause", "resume", "cancel", "recover"}
    assert "secret" not in json.dumps([t.model_dump() for t in tools.values()])


@pytest.mark.asyncio
async def test_invalid_run_id_fails_before_http(operations):
    _, server, request = operations
    with pytest.raises(Exception):
        await server.call_tool("get_loop_run", {"run_id": "../../admin"})
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_preserves_checkpoint_selection(operations):
    _, server, request = operations
    await server.call_tool("resume_coding_session", {"workspace": "/work/repo"})
    request.assert_awaited_once_with("POST", "/api/v1/coding/sessions/resume",
                                    json={"workspace": "/work/repo"}, timeout=120)


@pytest.mark.asyncio
async def test_review_and_deadline_validate_bounds(operations):
    _, server, request = operations
    await server.call_tool("review_coding_session", {"session_id": "job-1"})
    request.assert_awaited_with("POST", "/api/v1/coding/sessions/job-1/review", timeout=600)
    request.reset_mock()
    with pytest.raises(Exception):
        await server.call_tool("set_coding_deadline", {"session_id": "job-1", "minutes": 0})
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["decline", "cancel"])
async def test_admin_decline_never_mutates(operations, answer):
    _, server, request = operations
    ctx = SimpleNamespace(elicit=AsyncMock(return_value=SimpleNamespace(action=answer)))
    fn = server._tool_manager.get_tool("control_loop_run").fn
    result = await fn("test-run", "cancel", ctx)
    assert result == {"status": "cancelled", "executed": False}
    assert all(c.args[0] == "GET" for c in request.await_args_list)


@pytest.mark.asyncio
async def test_admin_consent_scopes_header_and_version(operations):
    _, server, request = operations
    ctx = SimpleNamespace(elicit=AsyncMock(return_value=SimpleNamespace(action="accept")))
    fn = server._tool_manager.get_tool("approve_loop_run").fn
    await fn("test-run", 2, ctx)
    request.assert_awaited_with("POST", "/api/v1/loop/runs/test-run/approve",
                               json={"expected_version": 2}, timeout=120,
                               headers={"X-Admin-Key": "test-service-secret"})
    assert "test-service-secret" not in ctx.elicit.await_args.kwargs["message"]
    assert '"version": 2' in ctx.elicit.await_args.kwargs["message"]
    assert all("headers" not in c.kwargs for c in request.await_args_list[:-1])


@pytest.mark.asyncio
async def test_run_changed_during_consent_refuses(operations):
    _, server, request = operations
    request.side_effect = [{"version": 2, "state": "draft"}, {"version": 3, "state": "draft"}]
    ctx = SimpleNamespace(elicit=AsyncMock(return_value=SimpleNamespace(action="accept")))
    with pytest.raises(ValueError, match="changed during approval"):
        await server._tool_manager.get_tool("approve_loop_run").fn("test-run", 2, ctx)
    assert all(c.args[0] == "GET" for c in request.await_args_list)


@pytest.mark.asyncio
async def test_benchmark_defaults_are_bounded_without_force(operations):
    _, server, request = operations
    await server.call_tool("start_benchmark", {"targets": ["red-radiance"]})
    body = request.await_args.kwargs["json"]
    assert body == {"targets": ["red-radiance"], "suites": ["performance"],
                    "limit": 3, "timeout_seconds": 300, "force": False, "keep_up": True}
    request.reset_mock()
    with pytest.raises(Exception):
        await server.call_tool("start_benchmark", {"targets": ["red-radiance"], "timeout_seconds": 10000})
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_benchmark_catalog_stops_after_health(operations):
    _, server, request = operations
    request.return_value = {"available": False}
    await server.call_tool("benchmark_catalog", {})
    request.assert_awaited_once_with("GET", "/api/v1/benchmarks/health")


@pytest.mark.asyncio
async def test_logs_have_bounded_pagination(operations):
    _, server, request = operations
    await server.call_tool("get_loop_logs", {"run_id": "test-run", "offset": 3, "limit": 10})
    request.assert_awaited_once_with("GET", "/api/v1/loop/runs/test-run/logs",
                                    params={"offset": 3, "limit": 10})


@pytest.mark.asyncio
async def test_stdio_consent_roundtrip_against_isolated_http_server(tmp_path):
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import ElicitResult
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def respond(self):
            calls.append((self.command, self.path, self.headers.get("X-Admin-Key")))
            body = json.dumps({"state": "draft", "version": 2}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_GET = respond
        do_POST = respond
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    key = tmp_path / "key"
    key.write_text("isolated-admin-key")
    answers = iter(["accept", "decline"])
    async def consent(context, params):
        assert "Approve this Aria Loop operation" in params.message
        assert "isolated-admin-key" not in params.message
        return ElicitResult(action=next(answers), content={})
    params = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).parents[2] / "mcp/server.py")],
        env={**os.environ, "ARIA_API_URL": f"http://127.0.0.1:{http.server_port}",
             "ARIA_API_KEY": "isolated-api-key", "ARIA_ADMIN_KEY_FILE": str(key)})
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, elicitation_callback=consent) as client:
                await client.initialize()
                accepted = await client.call_tool("approve_loop_run", {"run_id": "test-run", "expected_version": 2})
                assert not accepted.isError
                declined = await client.call_tool("control_loop_run", {"run_id": "test-run", "action": "cancel"})
                assert not declined.isError
        writes = [c for c in calls if c[0] == "POST"]
        assert writes == [("POST", "/api/v1/loop/runs/test-run/approve", "isolated-admin-key")]
        assert all(admin is None for method, _, admin in calls if method == "GET")
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)
