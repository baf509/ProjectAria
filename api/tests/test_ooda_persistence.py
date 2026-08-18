"""Tests for the OODA persistence contract (process_message_with_ooda).

The bug these pin: the OODA retry loop re-entered process_message per
attempt, which re-pushed the user message every time — one retry left
user → assistant(rejected) → user(duplicate) → assistant in the history
the next turn's context inherits, and double-incremented
stats.message_count. The contract now: the user message is persisted
exactly once, before the loop; only the ACCEPTED reply is stored.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from aria.core.ooda import OODALoop
from aria.core.orchestrator import Orchestrator
from aria.llm.base import Message

from tests.conftest import make_mock_db, FakeLLMAdapter

CONV_ID = str(ObjectId())
AGENT_ID = str(ObjectId())

DEFAULT_AGENT = {
    "_id": ObjectId(AGENT_ID),
    "slug": "default",
    "name": "Default Agent",
    "llm": {"backend": "llamacpp", "model": "default-model", "temperature": 0.7, "max_tokens": 4096},
    "capabilities": {"tools_enabled": False, "memory_enabled": False},
    "memory_config": {"auto_extract": False},
}

DEFAULT_CONVERSATION = {
    "_id": ObjectId(CONV_ID),
    "agent_id": AGENT_ID,
    "messages": [],
    "stats": {"message_count": 0},
}


class _PushRecorder:
    """Captures every $push into conversations.messages and every
    stats.message_count increment, in order."""

    def __init__(self):
        self.messages: list[dict] = []
        self.message_count_increments = 0

    async def update_one(self, query, update):
        for field, doc in update.get("$push", {}).items():
            if field == "messages":
                self.messages.append(doc)
        inc = update.get("$inc", {})
        if "stats.message_count" in inc:
            self.message_count_increments += inc["stats.message_count"]


def _make_orchestrator(db):
    orch = Orchestrator(db=db, tool_router=None)
    orch.context_builder = MagicMock()
    orch.context_builder.build_messages = AsyncMock(return_value=[
        Message(role="system", content="You are ARIA."),
        Message(role="user", content="hello"),
    ])
    orch.command_router = MagicMock()
    orch.command_router.try_handle = AsyncMock(return_value=None)
    orch.command_router.try_handle_contextual = AsyncMock(return_value=None)
    orch.memory_extractor = MagicMock()
    orch.memory_extractor.extract_from_conversation = AsyncMock(return_value=0)
    orch.long_term_memory = MagicMock()
    orch.usage_repo = MagicMock()
    orch.usage_repo.record = AsyncMock()
    return orch


def _wire_llm(orch, db, adapters):
    """Point the orchestrator's LLM at a scripted sequence of adapters
    (one per generation attempt) and stub the health bookkeeping."""
    from aria.core import orchestrator as orch_mod

    attempts = iter(adapters)
    mock_mgr = MagicMock()
    mock_mgr.get_adapter.side_effect = lambda *a, **k: next(attempts)
    mock_mgr.is_backend_healthy = AsyncMock(return_value=True)
    mock_mgr.record_backend_success = AsyncMock()
    mock_mgr.record_backend_failure = AsyncMock()
    mock_mgr.record_fallback = MagicMock()
    return patch.object(orch_mod, "llm_manager", mock_mgr)


def _script_evaluator(scores):
    """Patch OODALoop.evaluate_response with a scripted (score, feedback)
    sequence — one per attempt — so the test controls acceptance without
    mocking the evaluator's own LLM."""
    it = iter(scores)
    async def fake_evaluate(self, question, response, backend, model):
        return next(it)
    return patch.object(OODALoop, "evaluate_response", fake_evaluate)


async def _collect(aiter):
    out = []
    async for chunk in aiter:
        out.append(chunk)
    return out


@pytest.mark.asyncio
@patch("aria.core.orchestrator.hook_registry")
async def test_retry_persists_user_message_once(mock_hooks):
    """First attempt below threshold, second accepted: exactly one user
    message, one message_count increment, and only the accepted reply
    stored. The client sees only the accepted reply."""
    mock_hooks.fire = AsyncMock(return_value={})
    db = make_mock_db()
    recorder = _PushRecorder()
    db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
    db.conversations.update_one = recorder.update_one
    db.agents.find_one = AsyncMock(return_value=DEFAULT_AGENT)
    orch = _make_orchestrator(db)

    adapters = [
        FakeLLMAdapter(response_text="Meh, I guess."),
        FakeLLMAdapter(response_text="A proper answer."),
    ]
    with _wire_llm(orch, db, adapters), \
         _script_evaluator([(0.3, "too vague"), (0.9, "good")]):
        chunks = await _collect(orch.process_message_with_ooda(
            CONV_ID, "hello", {"threshold": 0.7, "max_retries": 2}
        ))

    user_msgs = [m for m in recorder.messages if m["role"] == "user"]
    assistant_msgs = [m for m in recorder.messages if m["role"] == "assistant"]
    assert len(user_msgs) == 1, f"expected exactly one user message, got: {recorder.messages}"
    # user + best assistant — exactly what a normal (non-ODA) turn records;
    # the retry added no extra increments.
    assert recorder.message_count_increments == 2
    assert len(assistant_msgs) == 1, "rejected attempts must not be persisted"
    assert assistant_msgs[0]["content"] == "A proper answer."

    text = "".join(c.content for c in chunks if c.type == "text")
    assert "A proper answer." in text
    assert "Meh, I guess." not in text  # the rejected reply never reaches the client


@pytest.mark.asyncio
@patch("aria.core.orchestrator.hook_registry")
async def test_all_attempts_failed_persists_best_effort_once(mock_hooks):
    """Every attempt below threshold: the best attempt is still returned to
    the client, so it is persisted (the user saw it — the next turn's
    context needs it). But it is persisted exactly ONCE, and the other
    rejected attempts are not. An empty result would persist nothing —
    an empty assistant message would poison the next turn's context."""
    mock_hooks.fire = AsyncMock(return_value={})
    db = make_mock_db()
    recorder = _PushRecorder()
    db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
    db.conversations.update_one = recorder.update_one
    db.agents.find_one = AsyncMock(return_value=DEFAULT_AGENT)
    orch = _make_orchestrator(db)

    adapters = [
        FakeLLMAdapter(response_text="weak one"),
        FakeLLMAdapter(response_text="weak two"),
    ]
    with _wire_llm(orch, db, adapters), \
         _script_evaluator([(0.2, "no"), (0.2, "no")]):
        chunks = await _collect(orch.process_message_with_ooda(
            CONV_ID, "hello", {"threshold": 0.7, "max_retries": 1}
        ))

    user_msgs = [m for m in recorder.messages if m["role"] == "user"]
    assistant_msgs = [m for m in recorder.messages if m["role"] == "assistant"]
    assert len(user_msgs) == 1
    assert recorder.message_count_increments == 2  # user + best attempt
    # The best attempt ("weak one" — the first, since 0.2 is not > 0.2)
    # is stored once; "weak two" is not.
    assert [m["content"] for m in assistant_msgs] == ["weak one"]
    assert any(c.type == "done" and c.usage.get("ooda_attempts") == 2 for c in chunks)


@pytest.mark.asyncio
@patch("aria.core.orchestrator.hook_registry")
async def test_plain_process_message_still_persists_both(mock_hooks):
    """The new flags default to True: a non-ODA turn persists the user
    message and the assistant reply exactly as before."""
    mock_hooks.fire = AsyncMock(return_value={})
    db = make_mock_db()
    recorder = _PushRecorder()
    db.conversations.find_one = AsyncMock(return_value=DEFAULT_CONVERSATION)
    db.conversations.update_one = recorder.update_one
    db.agents.find_one = AsyncMock(return_value=DEFAULT_AGENT)
    orch = _make_orchestrator(db)

    with _wire_llm(orch, db, [FakeLLMAdapter(response_text="Hi there!")]):
        await _collect(orch.process_message(CONV_ID, "hello"))

    roles = [m["role"] for m in recorder.messages]
    assert roles == ["user", "assistant"]
    assert recorder.message_count_increments == 2
