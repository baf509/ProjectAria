"""
ARIA - API Route Integration Tests

Tests for FastAPI routes using httpx.AsyncClient with mocked dependencies.
Does not require a running server or real MongoDB connection.
"""

import datetime as _dt_module
import importlib
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from httpx import ASGITransport, AsyncClient

from tests.conftest import make_mock_db

# ---------------------------------------------------------------------------
# datetime.UTC compatibility shim
#
# The application code does ``from datetime import datetime`` then calls
# ``datetime.now(datetime.UTC)``.  The ``UTC`` constant lives on the
# *module* (``datetime.UTC``) in Python >= 3.11, but it is NOT an attribute
# of the ``datetime`` *class* until Python 3.13+.  To let the existing app
# code work under Python 3.12 we patch each route module's ``datetime``
# reference with a thin subclass that exposes ``UTC``.
# ---------------------------------------------------------------------------


class _DatetimeWithUTC(datetime):
    """datetime subclass that exposes ``UTC`` as a class attribute."""

    UTC = _dt_module.timezone.utc


_MODULES_USING_DATETIME_UTC = [
    "aria.api.routes.conversations",
    "aria.api.routes.agents",
    "aria.api.routes.memories",
    "aria.api.routes.health",
    "aria.api.routes.usage",
    "aria.api.routes.admin",
]

for _modname in _MODULES_USING_DATETIME_UTC:
    _mod = importlib.import_module(_modname)
    if hasattr(_mod, "datetime") and not hasattr(_mod.datetime, "UTC"):
        _mod.datetime = _DatetimeWithUTC  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_OID = str(ObjectId())
VALID_OID_2 = str(ObjectId())
NOW = datetime.now(timezone.utc)


def _make_agent_doc(oid=None, slug="default", is_default=True):
    """Return a realistic agent MongoDB document."""
    return {
        "_id": ObjectId(oid) if oid else ObjectId(),
        "name": "Default Agent",
        "slug": slug,
        "description": "The default assistant",
        "system_prompt": "You are ARIA.",
        "mode_category": "chat",
        "greeting": "Hello!",
        "context_instructions": None,
        "llm": {
            "backend": "llamacpp",
            "model": "default",
            "temperature": 0.7,
            "max_tokens": 4096,
            "max_context_tokens": None,
            "force_non_streaming": False,
        },
        "fallback_chain": [],
        "capabilities": {
            "memory_enabled": True,
            "tools_enabled": False,
            "computer_use_enabled": False,
        },
        "mode_metadata": None,
        "memory_config": {
            "auto_extract": True,
            "short_term_messages": 20,
            "long_term_results": 10,
            "categories_filter": None,
        },
        "enabled_tools": [],
        "is_default": is_default,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _make_conversation_doc(oid=None, agent_id=None):
    """Return a realistic conversation MongoDB document."""
    return {
        "_id": ObjectId(oid) if oid else ObjectId(),
        "agent_id": ObjectId(agent_id) if agent_id else ObjectId(),
        "active_agent_id": None,
        "title": "Test Conversation",
        "summary": None,
        "status": "active",
        "created_at": NOW,
        "updated_at": NOW,
        "llm_config": {
            "backend": "llamacpp",
            "model": "default",
            "temperature": 0.7,
        },
        "messages": [],
        "tags": [],
        "pinned": False,
        "stats": {"message_count": 0, "total_tokens": 0, "tool_calls": 0},
    }


def _make_memory_doc(oid=None):
    """Return a realistic memory MongoDB document."""
    return {
        "_id": ObjectId(oid) if oid else ObjectId(),
        "content": "User prefers dark mode",
        "content_type": "preference",
        "categories": ["ui"],
        "importance": 0.7,
        "confidence": 1.0,
        "verified": False,
        "status": "active",
        "created_at": NOW,
        "source": {"type": "manual"},
        "access_count": 0,
    }


class _AsyncCursor:
    """Minimal async-iterable cursor mock with sort/skip/limit chaining."""

    def __init__(self, docs):
        self._docs = list(docs)
        self.sort = MagicMock(return_value=self)
        self.skip = MagicMock(return_value=self)
        self.limit = MagicMock(return_value=self)
        self.to_list = AsyncMock(return_value=self._docs)

    def __aiter__(self):
        return _AsyncCursorIterator(self._docs)


class _AsyncCursorIterator:
    def __init__(self, docs):
        self._it = iter(docs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def _make_async_cursor(docs):
    """Create a cursor that supports async iteration, sort, skip, limit."""
    return _AsyncCursor(docs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    return make_mock_db()


@pytest.fixture
def mock_tool_router():
    router = MagicMock()
    router.list_tools = MagicMock(return_value=[])
    router.get_tool = MagicMock(return_value=None)
    router.tool_count = MagicMock(return_value={"builtin": 0, "mcp": 0, "total": 0})
    return router


@pytest.fixture
def mock_orchestrator():
    return AsyncMock()


@pytest.fixture
async def client(mock_db, mock_tool_router, mock_orchestrator):
    """Create an httpx.AsyncClient with dependency overrides.

    Patches settings to disable api_key auth and the rate-limiter middleware
    so tests can hit endpoints without special headers.
    """
    from aria.main import app
    from aria.api import deps

    # Override FastAPI Depends-based dependencies
    app.dependency_overrides[deps.get_db] = lambda: mock_db
    app.dependency_overrides[deps.get_tool_router] = lambda: mock_tool_router
    app.dependency_overrides[deps.get_orchestrator] = lambda: mock_orchestrator
    # The admin-key split (steward plan §7.3) guards the irreversible routes —
    # PUT /agents, killswitch/estop deactivate, llm-route, model start/stop.
    # These tests exercise route LOGIC, so the gate is satisfied here and tested
    # on its own in TestAdminKeyGate below; otherwise every such test would be
    # asserting the auth layer instead of the behaviour it names.
    app.dependency_overrides[deps.require_admin] = lambda: True

    # The middleware calls get_rate_limiter() and reads settings directly (not
    # via Depends), so we patch them at the module level.
    mock_rate_limiter = MagicMock()
    mock_rate_limiter.check = MagicMock(return_value=(True, 100))

    with (
        patch("aria.main.settings") as mock_settings,
        patch("aria.main.get_rate_limiter", return_value=mock_rate_limiter),
    ):
        mock_settings.api_auth_enabled = False
        mock_settings.cors_origins = ["*"]

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac

    app.dependency_overrides.clear()


# ===================================================================
# Health
# ===================================================================


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client, mock_db):
        mock_db.command = AsyncMock(return_value={"ok": 1})
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        # In test env with no LLM/embeddings, status may be "degraded" rather than "healthy"
        assert data["status"] in ("healthy", "degraded")
        assert data["version"] == "0.2.0"
        assert data["database"] == "connected"

    @pytest.mark.asyncio
    async def test_health_returns_unhealthy_on_db_error(self, client, mock_db):
        mock_db.command = AsyncMock(side_effect=Exception("connection refused"))
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "unhealthy"
        assert "error" in data["database"]

    @pytest.mark.asyncio
    async def test_health_has_timestamp(self, client, mock_db):
        mock_db.command = AsyncMock(return_value={"ok": 1})
        resp = await client.get("/api/v1/health")
        assert "timestamp" in resp.json()


# ===================================================================
# Conversations
# ===================================================================


class TestListConversations:
    @pytest.mark.asyncio
    async def test_list_empty(self, client, mock_db):
        mock_db.conversations.find = MagicMock(
            return_value=_make_async_cursor([])
        )
        resp = await client.get("/api/v1/conversations")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_returns_conversations(self, client, mock_db):
        doc = _make_conversation_doc()
        doc.pop("messages", None)
        mock_db.conversations.find = MagicMock(
            return_value=_make_async_cursor([doc])
        )
        resp = await client.get("/api/v1/conversations")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["title"] == "Test Conversation"
        assert items[0]["status"] == "active"

    @pytest.mark.asyncio
    async def test_list_with_query_param(self, client, mock_db):
        mock_db.conversations.find = MagicMock(
            return_value=_make_async_cursor([])
        )
        resp = await client.get("/api/v1/conversations?q=hello&limit=10&skip=5")
        assert resp.status_code == 200
        mock_db.conversations.find.assert_called_once()
        call_args = mock_db.conversations.find.call_args[0][0]
        assert "$or" in call_args


class TestCreateConversation:
    @pytest.mark.asyncio
    async def test_create_with_default_agent(self, client, mock_db):
        agent_doc = _make_agent_doc()
        agent_oid = agent_doc["_id"]
        mock_db.agents.find_one = AsyncMock(return_value=agent_doc)
        mock_db.conversations.insert_one = AsyncMock(
            return_value=MagicMock(inserted_id=ObjectId())
        )

        resp = await client.post("/api/v1/conversations", json={})
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "New Conversation"
        assert data["agent_id"] == str(agent_oid)

    @pytest.mark.asyncio
    async def test_create_with_custom_title(self, client, mock_db):
        agent_doc = _make_agent_doc()
        mock_db.agents.find_one = AsyncMock(return_value=agent_doc)
        mock_db.conversations.insert_one = AsyncMock(
            return_value=MagicMock(inserted_id=ObjectId())
        )

        resp = await client.post(
            "/api/v1/conversations", json={"title": "My Chat"}
        )
        assert resp.status_code == 201
        assert resp.json()["title"] == "My Chat"

    @pytest.mark.asyncio
    async def test_create_agent_not_found(self, client, mock_db):
        mock_db.agents.find_one = AsyncMock(return_value=None)
        resp = await client.post("/api/v1/conversations", json={})
        assert resp.status_code == 404
        assert "Agent not found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_with_agent_id(self, client, mock_db):
        agent_doc = _make_agent_doc(oid=VALID_OID)
        mock_db.agents.find_one = AsyncMock(return_value=agent_doc)
        mock_db.conversations.insert_one = AsyncMock(
            return_value=MagicMock(inserted_id=ObjectId())
        )

        resp = await client.post(
            "/api/v1/conversations", json={"agent_id": VALID_OID}
        )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_create_returns_llm_config(self, client, mock_db):
        agent_doc = _make_agent_doc()
        mock_db.agents.find_one = AsyncMock(return_value=agent_doc)
        mock_db.conversations.insert_one = AsyncMock(
            return_value=MagicMock(inserted_id=ObjectId())
        )

        resp = await client.post("/api/v1/conversations", json={})
        assert resp.status_code == 201
        data = resp.json()
        assert "llm_config" in data
        assert data["llm_config"]["backend"] == "llamacpp"

    @pytest.mark.asyncio
    async def test_create_refuses_disabled_agent(self, client, mock_db):
        agent_doc = _make_agent_doc(slug="search-agent")
        agent_doc["enabled"] = False
        mock_db.agents.find_one = AsyncMock(return_value=agent_doc)

        resp = await client.post(
            "/api/v1/conversations", json={"agent_slug": "search-agent"}
        )
        assert resp.status_code == 400
        assert "disabled" in resp.json()["detail"]
        assert "search-agent" in resp.json()["detail"]


class TestGetConversation:
    @pytest.mark.asyncio
    async def test_get_existing(self, client, mock_db):
        doc = _make_conversation_doc(oid=VALID_OID)
        mock_db.conversations.find_one = AsyncMock(return_value=doc)

        resp = await client.get(f"/api/v1/conversations/{VALID_OID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == VALID_OID
        assert "messages" in data

    @pytest.mark.asyncio
    async def test_get_not_found(self, client, mock_db):
        mock_db.conversations.find_one = AsyncMock(return_value=None)
        resp = await client.get(f"/api/v1/conversations/{VALID_OID}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_invalid_id_returns_400(self, client, mock_db):
        # Invalid ObjectId format should return 400 Bad Request
        response = await client.get("/api/v1/conversations/not-a-valid-id")
        assert response.status_code == 400


class TestDeleteConversation:
    @pytest.mark.asyncio
    async def test_delete_existing(self, client, mock_db):
        mock_db.conversations.delete_one = AsyncMock(
            return_value=MagicMock(deleted_count=1)
        )
        resp = await client.delete(f"/api/v1/conversations/{VALID_OID}")
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_not_found(self, client, mock_db):
        mock_db.conversations.delete_one = AsyncMock(
            return_value=MagicMock(deleted_count=0)
        )
        resp = await client.delete(f"/api/v1/conversations/{VALID_OID}")
        assert resp.status_code == 404


class TestUpdateConversation:
    @pytest.mark.asyncio
    async def test_patch_title(self, client, mock_db):
        updated_doc = _make_conversation_doc(oid=VALID_OID)
        updated_doc["title"] = "Updated Title"
        mock_db.conversations.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1)
        )
        mock_db.conversations.find_one = AsyncMock(return_value=updated_doc)

        resp = await client.patch(
            f"/api/v1/conversations/{VALID_OID}",
            json={"title": "Updated Title"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Updated Title"

    @pytest.mark.asyncio
    async def test_patch_not_found(self, client, mock_db):
        mock_db.conversations.update_one = AsyncMock(
            return_value=MagicMock(matched_count=0)
        )
        resp = await client.patch(
            f"/api/v1/conversations/{VALID_OID}",
            json={"title": "x"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_empty_body(self, client, mock_db):
        resp = await client.patch(
            f"/api/v1/conversations/{VALID_OID}", json={}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_status(self, client, mock_db):
        updated_doc = _make_conversation_doc(oid=VALID_OID)
        updated_doc["status"] = "archived"
        mock_db.conversations.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1)
        )
        mock_db.conversations.find_one = AsyncMock(return_value=updated_doc)

        resp = await client.patch(
            f"/api/v1/conversations/{VALID_OID}",
            json={"status": "archived"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"


# ===================================================================
# Agents
# ===================================================================


class TestListAgents:
    @pytest.mark.asyncio
    async def test_list_empty(self, client, mock_db):
        mock_db.agents.find = MagicMock(return_value=_make_async_cursor([]))
        resp = await client.get("/api/v1/agents")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_returns_agents(self, client, mock_db):
        doc = _make_agent_doc()
        mock_db.agents.find = MagicMock(return_value=_make_async_cursor([doc]))
        resp = await client.get("/api/v1/agents")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["slug"] == "default"
        assert items[0]["name"] == "Default Agent"


class TestCreateAgent:
    @pytest.mark.asyncio
    async def test_create_success(self, client, mock_db):
        mock_db.agents.find_one = AsyncMock(return_value=None)
        new_oid = ObjectId()
        mock_db.agents.insert_one = AsyncMock(
            return_value=MagicMock(inserted_id=new_oid)
        )

        body = {
            "name": "Coder",
            "slug": "coder",
            "description": "A coding agent",
            "system_prompt": "You are a coder.",
            "llm": {
                "backend": "llamacpp",
                "model": "codellama",
                "temperature": 0.3,
                "max_tokens": 4096,
            },
            "capabilities": {
                "memory_enabled": True,
                "tools_enabled": True,
                "computer_use_enabled": False,
            },
            "memory_config": {
                "auto_extract": True,
                "short_term_messages": 20,
                "long_term_results": 10,
            },
        }
        resp = await client.post("/api/v1/agents", json=body)
        assert resp.status_code == 201
        data = resp.json()
        assert data["slug"] == "coder"
        assert data["name"] == "Coder"
        assert data["is_default"] is False

    @pytest.mark.asyncio
    async def test_create_duplicate_slug(self, client, mock_db):
        mock_db.agents.find_one = AsyncMock(return_value=_make_agent_doc())

        body = {
            "name": "Dupe",
            "slug": "default",
            "description": "Duplicate slug",
            "system_prompt": "prompt",
            "llm": {
                "backend": "llamacpp",
                "model": "default",
                "temperature": 0.7,
                "max_tokens": 4096,
            },
        }
        resp = await client.post("/api/v1/agents", json=body)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_create_missing_required_fields(self, client, mock_db):
        resp = await client.post("/api/v1/agents", json={"name": "incomplete"})
        assert resp.status_code == 422


class TestGetAgent:
    @pytest.mark.asyncio
    async def test_get_existing(self, client, mock_db):
        doc = _make_agent_doc(oid=VALID_OID)
        mock_db.agents.find_one = AsyncMock(return_value=doc)
        resp = await client.get(f"/api/v1/agents/{VALID_OID}")
        assert resp.status_code == 200
        assert resp.json()["id"] == VALID_OID

    @pytest.mark.asyncio
    async def test_get_not_found(self, client, mock_db):
        mock_db.agents.find_one = AsyncMock(return_value=None)
        resp = await client.get(f"/api/v1/agents/{VALID_OID}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_by_slug(self, client, mock_db):
        """A non-ObjectId path segment is looked up as a slug (MCP tools
        address agents by their stable slug, not the Mongo id)."""
        doc = _make_agent_doc(oid=VALID_OID, slug="pi-coding")

        async def fake_find_one(query):
            if query.get("slug") == "pi-coding":
                return doc
            return None

        mock_db.agents.find_one = AsyncMock(side_effect=fake_find_one)
        resp = await client.get("/api/v1/agents/pi-coding")
        assert resp.status_code == 200
        assert resp.json()["slug"] == "pi-coding"

    @pytest.mark.asyncio
    async def test_get_by_slug_not_found(self, client, mock_db):
        mock_db.agents.find_one = AsyncMock(return_value=None)
        resp = await client.get("/api/v1/agents/no-such-agent")
        assert resp.status_code == 404


class TestUpdateAgent:
    @pytest.mark.asyncio
    async def test_update_by_id(self, client, mock_db):
        doc = _make_agent_doc(oid=VALID_OID)
        mock_db.agents.find_one = AsyncMock(return_value=doc)
        mock_db.agents.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        resp = await client.put(f"/api/v1/agents/{VALID_OID}", json={"enabled": False})
        assert resp.status_code == 200
        mock_db.agents.update_one.assert_awaited_once()
        set_fields = mock_db.agents.update_one.call_args[0][1]["$set"]
        assert set_fields["enabled"] is False

    @pytest.mark.asyncio
    async def test_update_by_slug(self, client, mock_db):
        doc = _make_agent_doc(oid=VALID_OID, slug="search-agent")
        doc_id = doc["_id"]  # serialize_agent() pops "_id" from `doc` in place

        async def fake_find_one(query):
            if query.get("slug") == "search-agent" or query.get("_id") == doc_id:
                return doc
            return None

        mock_db.agents.find_one = AsyncMock(side_effect=fake_find_one)
        mock_db.agents.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        resp = await client.put(
            "/api/v1/agents/search-agent", json={"enabled": True}
        )
        assert resp.status_code == 200
        # Looked up by slug, but the actual $set targeted the resolved _id.
        filter_arg = mock_db.agents.update_one.call_args[0][0]
        assert filter_arg == {"_id": doc_id}

    @pytest.mark.asyncio
    async def test_update_not_found(self, client, mock_db):
        mock_db.agents.find_one = AsyncMock(return_value=None)
        resp = await client.put(f"/api/v1/agents/{VALID_OID}", json={"enabled": False})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_empty_body(self, client, mock_db):
        resp = await client.put(f"/api/v1/agents/{VALID_OID}", json={})
        assert resp.status_code == 400


class TestDeleteAgent:
    @pytest.mark.asyncio
    async def test_delete_non_default(self, client, mock_db):
        doc = _make_agent_doc(oid=VALID_OID, is_default=False)
        mock_db.agents.find_one = AsyncMock(return_value=doc)
        mock_db.agents.delete_one = AsyncMock(
            return_value=MagicMock(deleted_count=1)
        )
        resp = await client.delete(f"/api/v1/agents/{VALID_OID}")
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_default_agent_blocked(self, client, mock_db):
        doc = _make_agent_doc(oid=VALID_OID, is_default=True)
        mock_db.agents.find_one = AsyncMock(return_value=doc)
        resp = await client.delete(f"/api/v1/agents/{VALID_OID}")
        assert resp.status_code == 400
        assert "Cannot delete default agent" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_delete_not_found(self, client, mock_db):
        mock_db.agents.find_one = AsyncMock(return_value=None)
        mock_db.agents.delete_one = AsyncMock(
            return_value=MagicMock(deleted_count=0)
        )
        resp = await client.delete(f"/api/v1/agents/{VALID_OID}")
        assert resp.status_code == 404


# ===================================================================
# Memories
# ===================================================================


class TestListMemories:
    @pytest.mark.asyncio
    async def test_list_empty(self, client, mock_db):
        mock_db.memories.find = MagicMock(
            return_value=_make_async_cursor([])
        )
        resp = await client.get("/api/v1/memories")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_returns_memories(self, client, mock_db):
        doc = _make_memory_doc()
        mock_db.memories.find = MagicMock(
            return_value=_make_async_cursor([doc])
        )
        resp = await client.get("/api/v1/memories")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["content"] == "User prefers dark mode"
        assert items[0]["content_type"] == "preference"

    @pytest.mark.asyncio
    async def test_list_with_content_type_filter(self, client, mock_db):
        mock_db.memories.find = MagicMock(
            return_value=_make_async_cursor([])
        )
        resp = await client.get("/api/v1/memories?content_type=fact")
        assert resp.status_code == 200
        call_args = mock_db.memories.find.call_args[0][0]
        assert call_args["content_type"] == "fact"


class TestSearchMemories:
    @pytest.mark.asyncio
    async def test_search_success(self, client, mock_db):
        """Search endpoint calls LongTermMemory.search -- mock it."""
        with patch("aria.api.routes.memories.LongTermMemory") as MockLTM:
            mock_ltm_instance = MagicMock()

            fake_result = MagicMock()
            fake_result.id = VALID_OID
            fake_result.to_dict.return_value = {
                "id": VALID_OID,
                "content": "User likes Python",
                "content_type": "preference",
                "categories": ["programming"],
                "importance": 0.8,
                "confidence": 0.9,
                "verified": False,
                "created_at": NOW.isoformat(),
                "source": {"type": "extracted"},
            }
            mock_ltm_instance.search = AsyncMock(return_value=[fake_result])
            MockLTM.return_value = mock_ltm_instance

            mock_db.memories.find_one = AsyncMock(
                return_value=_make_memory_doc(oid=VALID_OID)
            )

            resp = await client.post(
                "/api/v1/memories/search",
                json={"query": "python preferences"},
            )
            assert resp.status_code == 200
            results = resp.json()
            assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_empty_query_validation(self, client, mock_db):
        resp = await client.post("/api/v1/memories/search", json={})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_search_with_filters(self, client, mock_db):
        with patch("aria.api.routes.memories.LongTermMemory") as MockLTM:
            mock_ltm_instance = MagicMock()
            mock_ltm_instance.search = AsyncMock(return_value=[])
            MockLTM.return_value = mock_ltm_instance

            resp = await client.post(
                "/api/v1/memories/search",
                json={
                    "query": "test",
                    "content_type": "fact",
                    "categories": ["programming"],
                    "limit": 5,
                },
            )
            assert resp.status_code == 200
            assert resp.json() == []


class TestGetMemory:
    @pytest.mark.asyncio
    async def test_get_existing(self, client, mock_db):
        doc = _make_memory_doc(oid=VALID_OID)
        mock_db.memories.find_one = AsyncMock(return_value=doc)
        resp = await client.get(f"/api/v1/memories/{VALID_OID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == VALID_OID
        assert data["content_type"] == "preference"

    @pytest.mark.asyncio
    async def test_get_not_found(self, client, mock_db):
        mock_db.memories.find_one = AsyncMock(return_value=None)
        resp = await client.get(f"/api/v1/memories/{VALID_OID}")
        assert resp.status_code == 404


# ===================================================================
# Tools
# ===================================================================


def _make_fake_tool(name="web", description="Search the web"):
    """Build a MagicMock that looks like a ToolDefinition."""
    fake_param = MagicMock()
    fake_param.name = "input"
    fake_param.type = "string"
    fake_param.description = "The input"
    fake_param.required = True
    fake_param.default = None
    fake_param.enum = None

    fake_tool = MagicMock()
    fake_tool.name = name
    fake_tool.description = description
    fake_tool.type = MagicMock(value="builtin")
    fake_tool.parameters = [fake_param]
    return fake_tool


class TestListTools:
    @pytest.mark.asyncio
    async def test_list_empty(self, client, mock_tool_router):
        mock_tool_router.list_tools.return_value = []
        resp = await client.get("/api/v1/tools")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_with_tools(self, client, mock_tool_router):
        mock_tool_router.list_tools.return_value = [_make_fake_tool()]

        resp = await client.get("/api/v1/tools")
        assert resp.status_code == 200
        tools = resp.json()
        assert len(tools) == 1
        assert tools[0]["name"] == "web"
        assert tools[0]["type"] == "builtin"
        assert len(tools[0]["parameters"]) == 1

    @pytest.mark.asyncio
    async def test_list_invalid_tool_type(self, client, mock_tool_router):
        resp = await client.get("/api/v1/tools?tool_type=invalid")
        assert resp.status_code == 400


class TestGetTool:
    @pytest.mark.asyncio
    async def test_get_not_found(self, client, mock_tool_router):
        mock_tool_router.get_tool.return_value = None
        resp = await client.get("/api/v1/tools/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_existing(self, client, mock_tool_router):
        mock_tool_router.get_tool.return_value = _make_fake_tool()

        resp = await client.get("/api/v1/tools/web")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "web"
        assert data["description"] == "Search the web"


# ===================================================================
# Root endpoint
# ===================================================================


class TestRoot:
    @pytest.mark.asyncio
    async def test_root_returns_info(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "ARIA"
        assert "version" in data
        assert data["docs"] == "/docs"


# ===================================================================
# Admin-key split (steward plan §7.3)
# ===================================================================


class TestAdminKeyGate:
    """The global API key must not be enough to take an irreversible action.

    Anything running as `ben` — including an unsandboxed coding agent — can read
    API_KEY out of .env. So the routes that change what future sessions do, or
    that lift a stop, sit behind ADMIN_KEY, which never enters a session
    environment. These tests deliberately do NOT override require_admin.
    """

    @pytest.fixture
    async def raw_client(self, mock_db, mock_tool_router, mock_orchestrator):
        from aria.main import app
        from aria.api import deps

        app.dependency_overrides[deps.get_db] = lambda: mock_db
        app.dependency_overrides[deps.get_tool_router] = lambda: mock_tool_router
        app.dependency_overrides[deps.get_orchestrator] = lambda: mock_orchestrator
        mock_rate_limiter = MagicMock()
        mock_rate_limiter.check = MagicMock(return_value=(True, 100))
        with (
            patch("aria.main.settings") as mock_settings,
            patch("aria.main.get_rate_limiter", return_value=mock_rate_limiter),
        ):
            mock_settings.api_auth_enabled = False
            mock_settings.cors_origins = ["*"]
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                yield ac
        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_unset_admin_key_refuses_rather_than_falling_back(self, raw_client):
        """Fails CLOSED. An unset ADMIN_KEY must not silently accept API_KEY —
        that would make the whole split cosmetic."""
        with patch("aria.config.settings.admin_key", ""):
            resp = await raw_client.put(
                f"/api/v1/agents/{VALID_OID}", json={"enabled": False}
            )
        assert resp.status_code == 503
        assert "ADMIN_KEY" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_wrong_admin_key_is_rejected(self, raw_client):
        with patch("aria.config.settings.admin_key", "s3cret-admin-key"):
            resp = await raw_client.put(
                f"/api/v1/agents/{VALID_OID}",
                json={"enabled": False},
                headers={"X-Admin-Key": "not-the-key"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_correct_admin_key_is_accepted(self, raw_client, mock_db):
        doc = _make_agent_doc(oid=VALID_OID)
        mock_db.agents.find_one = AsyncMock(return_value=doc)
        mock_db.agents.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        with patch("aria.config.settings.admin_key", "s3cret-admin-key"):
            resp = await raw_client.put(
                f"/api/v1/agents/{VALID_OID}",
                json={"enabled": False},
                headers={"X-Admin-Key": "s3cret-admin-key"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_killswitch_deactivate_is_gated(self, raw_client):
        """Lifting a stop is the decision; activating one stays open to anything."""
        with patch("aria.config.settings.admin_key", "s3cret-admin-key"):
            resp = await raw_client.post("/api/v1/killswitch/deactivate")
        assert resp.status_code == 403
