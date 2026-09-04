"""Gateway usage accounting must cover direct OpenAI-compatible traffic."""

import json
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from aria.api.routes import llm_proxy
from aria.infrastructure.preamble_fingerprint import PreambleTracker
from tests.conftest import make_mock_db


def _request(
    body: dict,
    *,
    caller: str | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
) -> Request:
    headers = [(b"user-agent", b"gateway-test/1.0")]
    if caller:
        headers.append((b"x-aria-caller", caller.encode()))
    if conversation_id:
        headers.append((b"x-aria-conversation-id", conversation_id.encode()))
    if session_id:
        headers.append((b"x-aria-session-id", session_id.encode()))
    raw = json.dumps(body).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/llm/v1/chat/completions",
        "raw_path": b"/llm/v1/chat/completions",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8200),
    }
    return Request(scope, receive)


def test_usage_counts_separates_prompt_cache_hits():
    payload = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 80},
        }
    }
    assert llm_proxy._usage_counts(payload) == (20, 7, 80, 100)


def test_usage_counts_supports_llama_timings_fallback():
    payload = {"timings": {"prompt_n": 40, "predicted_n": 6, "cache_n": 32}}
    assert llm_proxy._usage_counts(payload) == (8, 6, 32, 40)


def test_trace_timings_extracts_cache_speed_context_and_mtp():
    payload = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
        "timings": {
            "prompt_ms": 12.5,
            "prompt_per_second": 320.0,
            "predicted_ms": 400.0,
            "predicted_per_second": 50.0,
            "draft_n": 30,
            "draft_n_accepted": 21,
        },
    }

    trace = llm_proxy._trace_timings(payload)

    assert trace["context_tokens"] == 120
    assert trace["cache_hit_rate"] == 0.8
    assert trace["decode_tokens_per_second"] == 50.0
    assert trace["speculative_acceptance_rate"] == 0.7


def test_preamble_tracker_classifies_stability_and_timestamp_only_drift():
    tracker = PreambleTracker(max_entries=2)
    body = {
        "messages": [
            {"role": "system", "content": "Started 2026-09-04T10:11:12Z secret"}
        ],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
        "reasoning_effort": "medium",
    }

    first = tracker.observe("pi/model/chat", body)
    stable = tracker.observe("pi/model/chat", body)
    changed = tracker.observe(
        "pi/model/chat",
        {
            **body,
            "messages": [
                {"role": "system", "content": "Started 2026-09-04T10:59:01Z secret"}
            ],
        },
    )

    assert first["state"] == "first_seen"
    assert stable["state"] == "stable"
    assert changed["state"] == "changed"
    assert changed["change_reason"] == "volatile_timestamp"
    assert changed["tool_count"] == 1
    assert "secret" not in repr(first)
    assert "secret" not in repr(changed)


def test_preamble_tracker_classifies_tool_and_reasoning_drift():
    tracker = PreambleTracker()
    base = {
        "messages": [{"role": "system", "content": "stable"}],
        "tools": [{"type": "function", "function": {"name": "read"}}],
        "reasoning_effort": "low",
    }
    tracker.observe("key", base)
    changed = tracker.observe(
        "key",
        {
            **base,
            "tools": [{"type": "function", "function": {"name": "write"}}],
            "reasoning_effort": "medium",
        },
    )
    assert changed["change_reason"] == "tools_and_reasoning_template_changed"


def test_stream_usage_handles_split_sse_chunks_without_retaining_text():
    usage = llm_proxy._StreamUsage()
    usage.feed(b'data: {"choices":[{"delta":{"content":"secret text"}}]}\n')
    usage.feed(b'data: {"usage":{"prompt_tokens":12,"completion_')
    usage.feed(b'tokens":3,"prompt_tokens_details":{"cached_tokens":8}}}\n\n')
    usage.feed(b"data: [DONE]\n\n")
    usage.finish()

    assert usage.payload == {
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 8},
        }
    }
    assert "secret" not in repr(usage.payload)


def test_caller_priority_classifies_interactive_foreground_and_background():
    assert llm_proxy._caller_priority("hermes") == 0
    assert llm_proxy._caller_priority("pi-coding-mac") == 1
    assert llm_proxy._caller_priority("evalstack-benchmark") == 2


@pytest.mark.asyncio
async def test_admission_prioritizes_hermes_then_pi_then_background():
    admission = llm_proxy._PriorityAdmission(aging_seconds=60)
    first = await admission.acquire(1)
    assert first.queue_wait_ms == 0
    order: list[str] = []

    async def run(name: str, priority: int):
        await admission.acquire(priority)
        order.append(name)
        await admission.release()

    background = asyncio.create_task(run("background", 2))
    foreground = asyncio.create_task(run("foreground", 1))
    hermes = asyncio.create_task(run("hermes", 0))
    await asyncio.sleep(0)
    await admission.release()
    await asyncio.gather(background, foreground, hermes)

    assert order == ["hermes", "foreground", "background"]


@pytest.mark.asyncio
async def test_admission_aging_prevents_background_starvation():
    now = [0.0]
    admission = llm_proxy._PriorityAdmission(aging_seconds=10, clock=lambda: now[0])
    await admission.acquire(1)
    order: list[str] = []

    async def run(name: str, priority: int):
        await admission.acquire(priority)
        order.append(name)
        await admission.release()

    background = asyncio.create_task(run("aged-background", 2))
    await asyncio.sleep(0)
    now[0] = 21.0
    hermes = asyncio.create_task(run("new-hermes", 0))
    await asyncio.sleep(0)
    await admission.release()
    await asyncio.gather(background, hermes)

    assert order == ["aged-background", "new-hermes"]


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_consume_the_next_slot():
    admission = llm_proxy._PriorityAdmission(aging_seconds=60)
    await admission.acquire(1)
    cancelled = asyncio.create_task(admission.acquire(1))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await admission.release()

    next_request = await asyncio.wait_for(admission.acquire(1), timeout=0.2)
    assert next_request.controlled is True
    await admission.release()


@pytest.mark.asyncio
async def test_admission_only_controls_a_reported_single_slot_backend(monkeypatch):
    monkeypatch.setattr(llm_proxy.settings, "llm_proxy_admission_enabled", True)
    llm_proxy._admissions.clear()
    one_slot = llm_proxy._Route(
        "hybrid", "http://hybrid/v1", "pinned", [{"slug": "hybrid", "slots": 1}]
    )
    multi_slot = llm_proxy._Route(
        "other", "http://other/v1", "requested", [{"slug": "other", "slots": 4}]
    )

    assert llm_proxy._admission_for(one_slot) is not None
    assert llm_proxy._admission_for(multi_slot) is None
    snapshot = await llm_proxy._admission_snapshot(one_slot)
    assert snapshot["controlled"] is True
    assert snapshot["slots"] == 1
    assert snapshot["queued"] == 0


@pytest.mark.asyncio
async def test_nonstream_proxy_records_attributed_usage(monkeypatch):
    db = make_mock_db()
    route = llm_proxy._Route(
        "Qwen3.8-Flash-Next-Hybrid-R9700-Halo",
        "http://127.0.0.1:8004/v1",
        "pinned",
        [],
    )
    monkeypatch.setattr(llm_proxy, "_pick_backend", AsyncMock(return_value=route))
    monkeypatch.setattr(
        llm_proxy, "_backend_model_id_cached", AsyncMock(return_value="qwen-flash-next")
    )
    response = MagicMock(
        status_code=200,
        headers={"content-type": "application/json"},
        text="",
    )
    response.json.return_value = {
        "choices": [{"message": {"content": "not persisted"}}],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 12},
        },
        "timings": {
            "predicted_per_second": 51.5,
            "draft_n": 8,
            "draft_n_accepted": 6,
        },
    }
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(llm_proxy, "_client", lambda: client)

    result = await llm_proxy._proxy(
        "chat/completions",
        _request(
            {
                "model": "aria-resident",
                "messages": [
                    {"role": "system", "content": "private system prompt"},
                    {"role": "user", "content": "private prompt"},
                ],
                "tools": [
                    {"type": "function", "function": {"name": "private_tool"}}
                ],
            },
            caller="pi coding/mac",
            conversation_id="conversation-123",
            session_id="session-456",
        ),
        MagicMock(),
        db,
    )

    assert result.status_code == 200
    doc = db.usage.insert_one.call_args.args[0]
    assert doc["caller"] == "pi_coding/mac"
    assert doc["model"] == "Qwen3.8-Flash-Next-Hybrid-R9700-Halo"
    assert doc["source"] == "llm-gateway"
    assert doc["input_tokens"] == 8
    assert doc["cache_read_tokens"] == 12
    assert doc["output_tokens"] == 5
    assert doc["conversation_id"] == "conversation-123"
    assert doc["session_id"] == "session-456"
    assert len(doc["trace_id"]) == 32
    assert doc["preamble_hash"] == doc["metadata"]["preamble"]["fingerprint"]
    assert doc["metadata"]["status_code"] == 200
    assert doc["metadata"]["route_reason"] == "pinned"
    assert doc["metadata"]["queue_wait_ms"] == 0.0
    assert doc["metadata"]["admission_controlled"] is False
    assert doc["metadata"]["decode_tokens_per_second"] == 51.5
    assert doc["metadata"]["speculative_acceptance_rate"] == 0.75
    assert result.headers["x-aria-trace-id"] == doc["trace_id"]
    assert "private prompt" not in repr(doc)
    assert "private system prompt" not in repr(doc)
    assert "private_tool" not in repr(doc)
    assert "not persisted" not in repr(doc)


@pytest.mark.asyncio
async def test_stream_proxy_records_final_trace_and_first_chunk(monkeypatch):
    db = make_mock_db()
    route = llm_proxy._Route("hybrid", "http://127.0.0.1:8004/v1", "requested", [])
    monkeypatch.setattr(llm_proxy, "_pick_backend", AsyncMock(return_value=route))
    monkeypatch.setattr(
        llm_proxy, "_backend_model_id_cached", AsyncMock(return_value="qwen-flash-next")
    )

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_raw(self):
            yield b'data: {"choices":[{"delta":{"content":"private"}}]}\n\n'
            yield (
                b'data: {"usage":{"prompt_tokens":10,"completion_tokens":2,'
                b'"prompt_tokens_details":{"cached_tokens":8}},"timings":'
                b'{"predicted_per_second":40,"draft_n":4,"draft_n_accepted":3}}\n\n'
            )
            yield b"data: [DONE]\n\n"

    client = MagicMock()
    client.stream.return_value = Response()
    monkeypatch.setattr(llm_proxy, "_client", lambda: client)

    result = await llm_proxy._proxy(
        "chat/completions",
        _request(
            {
                "model": "hybrid",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "system", "content": "private stream system"}],
            },
            caller="hermes",
        ),
        MagicMock(),
        db,
    )
    chunks = [chunk async for chunk in result.body_iterator]

    assert chunks[-1] == b"data: [DONE]\n\n"
    doc = db.usage.insert_one.call_args.args[0]
    assert result.headers["x-aria-trace-id"] == doc["trace_id"]
    assert doc["metadata"]["streamed"] is True
    assert doc["metadata"]["first_chunk_ms"] is not None
    assert doc["metadata"]["cache_hit_rate"] == 0.8
    assert doc["metadata"]["speculative_acceptance_rate"] == 0.75
    assert "private stream system" not in repr(doc)
