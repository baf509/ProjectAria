"""
Tests for aria.db.models (Pydantic models).
"""

import pytest
from datetime import datetime
from pydantic import ValidationError

from aria.db.models import (
    MessageRequest,
    ConversationCreate,
    ConversationUpdate,
    AgentCreate,
    AgentLLMConfig,
    ResearchCreate,
    ConversationBranch,
    HealthResponse,
    CodingSessionResponse,
    SteeringMessageRequest,
)


class TestMessageRequest:
    def test_defaults(self):
        req = MessageRequest(content="hello")
        assert req.content == "hello"
        assert req.stream is True


class TestConversationCreate:
    def test_defaults(self):
        conv = ConversationCreate()
        assert conv.agent_id is None
        assert conv.agent_slug is None
        assert conv.title is None
        assert conv.private is False


class TestConversationUpdate:
    def test_all_optional(self):
        update = ConversationUpdate()
        assert update.title is None
        assert update.status is None
        assert update.tags is None
        assert update.pinned is None
        assert update.active_agent_id is None
        assert update.private is None


class TestAgentCreate:
    def test_required_fields(self):
        llm = AgentLLMConfig(backend="openai", model="gpt-4")
        agent = AgentCreate(
            name="Test",
            slug="test",
            description="A test agent",
            system_prompt="You are a test agent.",
            llm=llm,
        )
        assert agent.name == "Test"
        assert agent.slug == "test"
        assert agent.description == "A test agent"
        assert agent.system_prompt == "You are a test agent."
        assert agent.llm.backend == "openai"

    def test_missing_required_raises(self):
        with pytest.raises(ValidationError):
            AgentCreate(name="Test")  # missing slug, description, system_prompt, llm


class TestAgentLLMConfig:
    def test_defaults(self):
        cfg = AgentLLMConfig(backend="openai", model="gpt-4")
        assert cfg.temperature == 0.7
        assert cfg.max_tokens == 4096


class TestResearchCreate:
    def test_depth_bounds(self):
        r = ResearchCreate(query="test", depth=1)
        assert r.depth == 1

        r = ResearchCreate(query="test", depth=4)
        assert r.depth == 4

        with pytest.raises(ValidationError):
            ResearchCreate(query="test", depth=0)

        with pytest.raises(ValidationError):
            ResearchCreate(query="test", depth=5)


class TestConversationBranch:
    def test_index_non_negative(self):
        branch = ConversationBranch(message_index=0)
        assert branch.message_index == 0

        with pytest.raises(ValidationError):
            ConversationBranch(message_index=-1)


class TestHealthResponse:
    def test_all_fields(self):
        now = datetime.utcnow()
        h = HealthResponse(
            status="healthy",
            version="1.0.0",
            database="connected",
            timestamp=now,
            embeddings="ok",
            llm="ok",
        )
        assert h.status == "healthy"
        assert h.version == "1.0.0"
        assert h.database == "connected"
        assert h.timestamp == now
        assert h.embeddings == "ok"
        assert h.llm == "ok"


class TestCodingSessionResponse:
    def test_all_fields(self):
        now = datetime.utcnow()
        cs = CodingSessionResponse(
            id="cs-1",
            backend="claude",
            workspace="/tmp/ws",
            prompt="fix bug",
            status="running",
            created_at=now,
            updated_at=now,
        )
        assert cs.id == "cs-1"
        assert cs.backend == "claude"
        assert cs.workspace == "/tmp/ws"
        assert cs.prompt == "fix bug"
        assert cs.status == "running"
        assert cs.model is None
        assert cs.branch is None
        assert cs.pid is None
        assert cs.visible is False
        assert cs.tmux_pane_id is None
        assert cs.completed_at is None


class TestSteeringMessageRequest:
    def test_defaults(self):
        req = SteeringMessageRequest(content="stop")
        assert req.content == "stop"
        assert req.priority == "normal"
