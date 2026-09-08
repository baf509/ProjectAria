"""Exercise Aria's real provider conversions; only the network transport is fake."""
import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionChunk

from aria.llm.anthropic import AnthropicAdapter
from aria.llm.base import ToolCall
from aria.llm.llamacpp import LlamaCppAdapter
from aria.llm.openai import OpenAIAdapter
from aria.llm.openrouter import OpenRouterAdapter
from aria.ralph.models import WorkerReport
from aria.ralph.worker import AgentWorker, structured_reply
from tests.test_ralph import fixture, create, finish


REPORT = {"outcome": "ready", "changed": "Updated answer", "checks": "development check", "handoff": ""}


async def test_local_adapter_stream_roundtrip_with_tool_history_and_reasoning(fixture):
    service, _, _ = fixture
    requests = []

    async def completion(**request):
        requests.append(request)
        if len(requests) == 1:
            deltas = [{"role": "assistant", "tool_calls": [{
                "index": 0, "id": "edit", "type": "function", "function": {
                    "name": "shell", "arguments": json.dumps({"command": "printf 2 > /workspace/answer.txt"}),
                },
            }]}]
        else:
            call = request["messages"][2]["tool_calls"][0]
            assert call["function"]["name"] == "shell"
            assert json.loads(call["function"]["arguments"])["command"].startswith("printf 2")
            assert request["messages"][3]["tool_call_id"] == "edit"
            deltas = [{"role": "assistant", "reasoning_content": "Check the supplied criteria."},
                      {"content": "```json\n" + json.dumps(REPORT) + "\n```"}]

        async def stream():
            for index, delta in enumerate(deltas):
                yield ChatCompletionChunk.model_validate({
                    "id": "fake", "created": 0, "object": "chat.completion.chunk", "model": "fake-local",
                    "choices": [{"index": 0, "delta": delta,
                                 "finish_reason": "stop" if index == len(deltas) - 1 else None}],
                })
            yield ChatCompletionChunk.model_validate({
                "id": "fake", "created": 0, "object": "chat.completion.chunk", "model": "fake-local",
                "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            })
        return stream()

    # The real LlamaCppAdapter inherits OpenAIAdapter's streaming, conversion,
    # and accounting. Skip credential/client construction, replace only HTTP.
    adapter = LlamaCppAdapter.__new__(LlamaCppAdapter)
    adapter.model = "fake-local"
    adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    service.worker = AgentWorker(lambda *_: adapter)
    result = await finish(service, (await create(service))["_id"])
    assert result["state"] == "ready_for_review", result["stop_reason"]
    assert len(requests) == 2
    assert result["usage"]["reported_tokens"] == 30


@pytest.mark.parametrize("adapter_class", [OpenAIAdapter, OpenRouterAdapter, AnthropicAdapter])
async def test_worker_history_uses_existing_provider_neutral_contract(fixture, adapter_class):
    service, _, _ = fixture
    histories = []
    converter = adapter_class.__new__(adapter_class)

    class Provider:
        async def complete(self, messages, **kwargs):
            histories.append(converter._convert_messages(messages))
            if len(messages) == 2:
                return "Implement", [ToolCall("edit", "shell", {"command": "printf 2 > /workspace/answer.txt"})], {"total_tokens": 1}
            return json.dumps(REPORT), [], {"total_tokens": 1}

    service.worker = AgentWorker(lambda *_: Provider())
    result = await finish(service, (await create(service))["_id"])
    assert result["state"] == "ready_for_review", result["stop_reason"]
    assert len(histories) == 2


@pytest.mark.parametrize("content", [
    "done", json.dumps(REPORT) + json.dumps(REPORT),
    json.dumps({**REPORT, "state": "verified"}),
    "<think>unfinished " + json.dumps(REPORT),
])
def test_reply_wrappers_do_not_relax_result_schema(content):
    with pytest.raises(ValueError):
        WorkerReport.model_validate_json(structured_reply(content))
