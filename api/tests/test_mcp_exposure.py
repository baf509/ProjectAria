"""Operator diagnostics must be bounded, read-only, truthful and usable by MCP."""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tests.test_mcp import _load_bridge_server


@pytest.fixture
def bridge():
    return _load_bridge_server()


def test_route_distinction_survives_short_catalog_description(bridge):
    # Hermes exposes at most 60 characters / first sentence in its native
    # discovery catalog. The distinction must not depend on a later warning.
    named = bridge.inference_backend.__doc__.splitlines()[0].strip()
    default = bridge.get_llm_route.__doc__.splitlines()[0].strip()
    assert len(named) <= 60 and len(default) <= 60
    assert "named model" in named and "admission queue" in named
    assert "Default routing policy only" in default
    assert "not a named model's backend" in default


@pytest.mark.asyncio
async def test_contract_identifies_loaded_source_not_replaced_disk(bridge):
    before = await bridge.tool_contract_status()
    with patch.object(bridge.Path, "read_bytes", return_value=b"replacement not loaded"):
        assert await bridge.tool_contract_status() == before


@pytest.mark.asyncio
async def test_http_failures_do_not_echo_sensitive_upstream_bodies(bridge):
    async def response(request):
        assert request.headers["X-API-Key"] == "test-key"
        return httpx.Response(403, text="secret-key and private transcript")
    bridge.ARIA_KEY = "test-key"
    client = httpx.AsyncClient(base_url="http://aria.test", headers={"X-API-Key": bridge.ARIA_KEY},
                               transport=httpx.MockTransport(response))
    with patch.object(bridge, "_client", return_value=client):
        with pytest.raises(bridge.AriaRequestError) as error:
            await bridge.inference_backend("explicit-model")
    assert error.value.status_code == 403
    assert "secret-key" not in str(error.value)
    assert "private transcript" not in str(error.value)
    assert "do not bypass auth" in str(error.value)


@pytest.mark.asyncio
async def test_operator_snapshot_parallel_reads_and_partial_failure(bridge):
    entered = set()
    all_entered = asyncio.Event()
    async def read(method, path, **kwargs):
        assert method == "GET"
        entered.add(path)
        if len(entered) == 5:
            all_entered.set()
        await asyncio.wait_for(all_entered.wait(), 1)
        if path.endswith("/availability"):
            raise bridge.AriaRequestError("GET", path, 503)
        if path.endswith("/backend"):
            assert kwargs["params"] == {"model": "explicit-model"}
            return {"admission": {"queued": 2}}
        return {"healthy": False}
    bridge._request = AsyncMock(side_effect=read)
    result = await bridge.operator_snapshot("explicit-model")
    assert result["complete"] is False
    assert result["sections"]["readiness"] == {"available": True, "data": {"healthy": False}}
    assert result["sections"]["coding_provider"]["available"] is False
    assert result["sections"]["coding_provider"]["status_code"] == 503
    assert result["sections"]["inference"]["data"]["admission"]["queued"] == 2


@pytest.mark.asyncio
async def test_probe_timeout_is_unknown_and_cancellation_propagates(bridge):
    bridge._request = AsyncMock(side_effect=httpx.ReadTimeout("secret-body"))
    result = await bridge._read_section("/health")
    assert result == {"available": False, "error_type": "ReadTimeout", "status_code": None}
    bridge._request.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await bridge._read_section("/health")


@pytest.mark.asyncio
@pytest.mark.parametrize("group,path", [("summary", "summary"), ("caller", "by-caller"), ("model", "by-model")])
async def test_usage_allowlist(bridge, group, path):
    bridge._request = AsyncMock(return_value=[])
    assert await bridge.inference_usage(group, 2) == {"group_by": group, "days": 2, "data": []}
    bridge._request.assert_awaited_once_with("GET", f"/api/v1/usage/{path}", params={"days": 2})


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,kwargs", [
    ("inference_usage", {"days": 0}), ("inference_usage", {"days": 31}),
    ("inference_usage", {"group_by": "../../admin"}),
    ("inference_traces", {"limit": 201}), ("inference_traces", {"hours": 721}),
    ("inference_traces", {"caller": "x" * 121}), ("inference_traces", {"limit": True}),
    ("get_task", {"task_id": "../../keys"}), ("get_task", {"id": "task?other=yes"}),
    ("get_task", {"task_id": "a", "id": "b"}), ("get_task", {}),
    ("benchmark_status", {"limit": -1}), ("benchmark_status", {"run_id": "../../admin"}),
    ("ralph_status", {"run_id": "https://untrusted"}), ("ralph_status", {"limit": 101}),
])
async def test_bad_inputs_never_reach_api(bridge, tool, kwargs):
    bridge._request = AsyncMock()
    with pytest.raises(ValueError):
        await getattr(bridge, tool)(**kwargs)
    bridge._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_alias_and_trace_filters(bridge):
    bridge._request = AsyncMock(return_value={})
    await bridge.get_task(id="task-123")
    bridge._request.assert_awaited_with("GET", "/api/v1/todos/task-123")
    bridge._request.return_value = [{"trace_id": "one"}, {"trace_id": "two"}]
    result = await bridge.inference_traces(hours=2, limit=3, caller="hermes")
    assert result == {"traces": [{"trace_id": "one"}, {"trace_id": "two"}], "returned": 2}
    bridge._request.assert_awaited_with("GET", "/api/v1/usage/traces",
                                      params={"hours": 2, "limit": 3, "caller": "hermes"})


@pytest.mark.asyncio
async def test_unavailable_benchmark_is_not_empty_success(bridge):
    bridge._request = AsyncMock(return_value={"available": False, "root": "private"})
    assert await bridge.benchmark_status() == {"available": False, "reason": "evalstack unavailable", "runs": None}
    assert bridge._request.await_count == 1


@pytest.mark.asyncio
async def test_benchmark_projection_bounds_and_redacts_logs(bridge):
    bridge._request = AsyncMock(side_effect=[{"available": True}, {
        "run_id": "b-1", "status": "succeeded", "argv": ["secret"], "log_tail": "transcript",
        "metrics": [{"value": 42, "metric": "tg", "prompt": "private"}, {"value": 43}],
    }])
    result = await bridge.benchmark_status("b-1", limit=1)
    assert result == {"available": True, "run": {"run_id": "b-1", "status": "succeeded"},
                      "metrics": [{"value": 42, "metric": "tg"}], "metrics_total": 2, "truncated": True}
    assert all(call.args[0] == "GET" for call in bridge._request.await_args_list)


@pytest.mark.asyncio
async def test_ralph_projection_never_exposes_plan_or_attempts(bridge):
    row = {"_id": "r1", "state": "paused", "limits": {"max_attempts": 2},
           "plan": "private prompt", "attempts": ["transcript"], "events": ["log"]}
    bridge._request = AsyncMock(return_value=[row, row])
    result = await bridge.ralph_status(limit=1)
    assert result["truncated"] is True
    assert result["runs"] == [{"_id": "r1", "state": "paused", "limits": {"max_attempts": 2}}]
    bridge._request.return_value = row
    assert (await bridge.ralph_status("r1"))["runs"] == result["runs"]
    assert all(call.args[0] == "GET" for call in bridge._request.await_args_list)


def test_description_contract_does_not_invent_host_or_client_identity(bridge):
    assert "RTX 3090" in bridge.list_gpu_devices.__doc__
    assert "Other hosts" in bridge.list_gpu_devices.__doc__
    assert "NOT necessarily" in bridge.get_llm_route.__doc__
    assert "do not necessarily own" in bridge.model_server_utilization.__doc__
    assert "Admin authorization" in bridge.set_llm_route.__doc__
    assert "default route, NOT caller identity" in bridge.operator_snapshot.__doc__
