"""MCP knowledge/diagnostic additions: writes, bounds and polling semantics."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from tests.test_mcp import _load_bridge_server


@pytest.fixture
def bridge():
    return _load_bridge_server()


@pytest.mark.asyncio
async def test_memory_correction_preserves_false_zero_and_empty_categories(bridge):
    bridge._request = AsyncMock(return_value={"verified": False})
    result = await bridge.update_memory("a" * 24, verified=False, importance=0, categories=[])
    assert result == {"verified": False}
    bridge._request.assert_awaited_once_with("PATCH", "/api/v1/memories/" + "a" * 24,
        json={"verified": False, "importance": 0, "categories": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,kwargs", [
    ("get_memory", {"memory_id": "../../admin"}),
    ("update_memory", {"memory_id": "a" * 24}),
    ("update_memory", {"memory_id": "a" * 24, "content": "  "}),
    ("store_memory", {"content": "  "}),
    ("get_model_server", {"slug": "../llm-route"}),
    ("get_research_report", {"run_id": "a?admin=yes"}),
    ("wait_for_shell_output", {"name": "../input", "since_line": 0}),
])
async def test_bad_identifiers_and_empty_writes_never_reach_api(bridge, tool, kwargs):
    bridge._request = AsyncMock()
    with pytest.raises(ValueError):
        await getattr(bridge, tool)(**kwargs)
    bridge._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_provenance_and_recall_categories(bridge):
    bridge._request = AsyncMock(return_value={"id": "a" * 24})
    await bridge.store_memory("Evidence", confidence=0.7, private=True, source_ref="task:123")
    method, path = bridge._request.call_args.args
    body = bridge._request.call_args.kwargs["json"]
    assert (method, path) == ("POST", "/api/v1/memory/store")
    assert body["source"] == {"type": "agent", "via": "aria_mcp", "reference": "task:123"}
    assert body["confidence"] == 0.7 and body["private"] is True
    assert "verified" not in body
    await bridge.search_memory("query", categories=["Aria"])
    assert bridge._request.call_args.kwargs["json"]["categories"] == ["Aria"]


@pytest.mark.asyncio
async def test_temperatures_keep_missing_and_stale_evidence(bridge):
    rows = [{"node": "mac", "status": "unavailable", "sensors": [], "observed_at": None}]
    bridge._request = AsyncMock(return_value={"temperature_hosts": rows})
    assert await bridge.host_temperatures("mac") == {"available": False, "hosts": rows}
    bridge._request.return_value = {}
    assert (await bridge.host_temperatures())["available"] is False


@pytest.mark.asyncio
async def test_research_summaries_exclude_reports_and_reports_page_without_loss(bridge):
    bridge._request = AsyncMock(return_value=[{"id": "r1", "status": "running", "report_text": "private"}] * 2)
    assert await bridge.research_status(limit=1) == {
        "runs": [{"id": "r1", "status": "running"}], "returned": 1, "truncated": True}
    bridge._request.return_value = {"status": "completed", "report_text": "abcdef"}
    one = await bridge.get_research_report("r1", limit=3)
    two = await bridge.get_research_report("r1", offset=one["next_offset"], limit=3)
    assert one["text"] + two["text"] == "abcdef" and two["next_offset"] is None
    bridge._request.return_value = {"status": "running", "report_text": None}
    assert (await bridge.get_research_report("r1"))["available"] is False


@pytest.mark.asyncio
async def test_wait_polling_cursor_and_zero_timeout(bridge):
    bridge._request = AsyncMock(side_effect=[{"events": []},
        {"events": [{"line_number": 12, "text_clean": "output"}], "has_more": True}])
    result = await bridge.wait_for_shell_output("claude-test", 10, timeout_seconds=2)
    assert result["next_line"] == 12 and result["has_more"] and not result["timed_out"]
    assert all(call.args[0] == "GET" for call in bridge._request.await_args_list)
    assert bridge._request.call_args.kwargs["params"]["kinds"] == "output"
    bridge._request = AsyncMock(return_value={"events": []})
    result = await bridge.wait_for_shell_output("claude-test", 12, timeout_seconds=0)
    assert result["timed_out"] and result["capture_available"] and result["next_line"] == 12
    bridge._request.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_bounds_slow_reads_and_propagates_cancellation(bridge):
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)
    bridge._request = AsyncMock(side_effect=slow)
    result = await bridge.wait_for_shell_output("claude-test", 0, timeout_seconds=0.01)
    assert result["timed_out"] and not result["capture_available"]
    bridge._request = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await bridge.wait_for_shell_output("claude-test", 0)


@pytest.mark.asyncio
async def test_real_mcp_validation_rejects_unbounded_requests_before_http():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('knowledge_real_bridge', Path(__file__).parents[2] / 'mcp/server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._request = AsyncMock()
    for name, arguments in [
        ('wait_for_shell_output', {'name': 'test', 'since_line': -1}),
        ('wait_for_shell_output', {'name': 'test', 'since_line': 0, 'timeout_seconds': 31}),
        ('list_memories', {'limit': 0}), ('list_memories', {'limit': 101}),
        ('awareness_observations', {'hours': 169}),
        ('awareness_observations', {'severity': 'invented'}),
        ('store_memory', {'content': 'x', 'confidence': 1.1}),
        ('update_memory', {'memory_id': 'a' * 24, 'importance': -1}),
        ('get_research_report', {'run_id': 'r1', 'limit': 20001}),
    ]:
        with pytest.raises(Exception):
            await module.mcp.call_tool(name, arguments)
    module._request.assert_not_awaited()
    tools = {t.name: t for t in await module.mcp.list_tools()}
    assert tools['wait_for_shell_output'].annotations.readOnlyHint
    assert not tools['store_memory'].annotations.readOnlyHint
    assert tools['update_memory'].annotations.destructiveHint


@pytest.mark.asyncio
async def test_resize_returns_structured_success_for_no_content_response(bridge):
    bridge._request = AsyncMock(return_value=None)
    assert await bridge.resize_shell('canary', 100, 30) == {'ok': True, 'name': 'canary', 'cols': 100, 'rows': 30}


@pytest.mark.asyncio
async def test_literal_input_submits_separately_and_observes_after_enter(bridge):
    bridge._request = AsyncMock(side_effect=[{'ok': True}, {'ok': True, 'screen': 'executed'}])
    result = await bridge.send_shell_input('canary', 'echo Enter', literal=True, wait_ms=500)
    calls = bridge._request.await_args_list
    assert calls[0].kwargs['json'] == {'text': 'echo Enter', 'literal': True, 'append_enter': False, 'wait_ms': 0}
    assert calls[1].kwargs['json'] == {'text': '', 'literal': False, 'append_enter': True, 'wait_ms': 500}
    assert result['screen'] == 'executed'
    bridge._request = AsyncMock(side_effect=RuntimeError('typing failed'))
    with pytest.raises(RuntimeError):
        await bridge.send_shell_input('canary', 'echo Enter', literal=True)
    bridge._request.assert_awaited_once()


@pytest.mark.asyncio
async def test_literal_input_can_still_leave_text_unsubmitted(bridge):
    bridge._request = AsyncMock(return_value={'ok': True})
    await bridge.send_shell_input('canary', 'draft', literal=True, append_enter=False)
    bridge._request.assert_awaited_once()
    assert bridge._request.call_args.kwargs['json']['append_enter'] is False


@pytest.mark.asyncio
async def test_missing_snapshot_is_normal_unavailability_not_circuit_breaker_error(bridge):
    bridge._request = AsyncMock(side_effect=bridge.AriaRequestError('GET', '/snapshot', 404))
    assert (await bridge.get_shell_snapshot('new-shell'))['available'] is False
    bridge._request.side_effect = bridge.AriaRequestError('GET', '/snapshot', 403)
    with pytest.raises(bridge.AriaRequestError):
        await bridge.get_shell_snapshot('new-shell')


@pytest.mark.asyncio
async def test_close_preserves_pending_result_and_reports_no_content_success(bridge):
    bridge._request = AsyncMock(return_value=None)
    assert await bridge.delete_shell('canary') == {'status': 'completed', 'name': 'canary', 'purge': False}
    bridge._request.return_value = {'status': 'pending', 'command_id': 'remote'}
    assert (await bridge.delete_shell('canary'))['status'] == 'pending'
