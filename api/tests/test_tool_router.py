"""Tests for aria.tools.router — tool registration, policy, and execution."""

import asyncio
from unittest.mock import patch

import pytest

from aria.tools.base import ToolResult, ToolStatus, ToolType
from aria.tools.router import ToolRouter

from tests.conftest import FakeTool


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestToolRegistration:
    def test_register_and_retrieve(self, tool_router):
        tool = FakeTool(tool_name="test_tool")
        tool_router.register_tool(tool)
        assert tool_router.has_tool("test_tool")
        assert tool_router.get_tool("test_tool") is tool

    def test_duplicate_registration_raises(self, tool_router):
        tool = FakeTool(tool_name="dup")
        tool_router.register_tool(tool)
        with pytest.raises(ValueError, match="already registered"):
            tool_router.register_tool(FakeTool(tool_name="dup"))

    def test_unregister(self, tool_router):
        tool = FakeTool(tool_name="rm_tool")
        tool_router.register_tool(tool)
        assert tool_router.unregister_tool("rm_tool") is True
        assert tool_router.has_tool("rm_tool") is False

    def test_unregister_nonexistent(self, tool_router):
        assert tool_router.unregister_tool("nope") is False

    def test_tool_count(self, tool_router):
        tool_router.register_tool(FakeTool(tool_name="a"))
        tool_router.register_tool(FakeTool(tool_name="b"))
        counts = tool_router.tool_count()
        assert counts["total"] == 2
        assert counts["builtin"] == 2

    def test_list_tools(self, tool_router):
        tool_router.register_tool(FakeTool(tool_name="x"))
        tools = tool_router.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "x"

    def test_clear_tools(self, tool_router):
        tool_router.register_tool(FakeTool(tool_name="c1"))
        tool_router.register_tool(FakeTool(tool_name="c2"))
        cleared = tool_router.clear_tools()
        assert cleared == 2
        assert tool_router.tool_count()["total"] == 0


# ---------------------------------------------------------------------------
# Tool definitions for LLM
# ---------------------------------------------------------------------------

class TestToolDefinitions:
    def test_get_all_definitions(self, tool_router):
        tool_router.register_tool(FakeTool(tool_name="t1"))
        tool_router.register_tool(FakeTool(tool_name="t2"))
        defs = tool_router.get_tool_definitions()
        assert len(defs) == 2
        names = {d["name"] for d in defs}
        assert names == {"t1", "t2"}

    def test_filter_by_enabled(self, tool_router):
        tool_router.register_tool(FakeTool(tool_name="t1"))
        tool_router.register_tool(FakeTool(tool_name="t2"))
        defs = tool_router.get_tool_definitions(enabled_tools=["t1"])
        assert len(defs) == 1
        assert defs[0]["name"] == "t1"

    def test_definition_structure(self, tool_router):
        tool_router.register_tool(FakeTool(tool_name="t1"))
        defs = tool_router.get_tool_definitions()
        d = defs[0]
        assert "name" in d
        assert "description" in d
        assert "parameters" in d
        assert d["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class TestToolExecution:
    @pytest.mark.asyncio
    async def test_execute_success(self, tool_router):
        tool = FakeTool(tool_name="web", result="page content")
        tool_router.register_tool(tool)

        with patch.object(tool_router, "_is_tool_allowed", return_value=(True, None)):
            result = await tool_router.execute_tool("web", {"input": "test"})

        assert result.status == ToolStatus.SUCCESS
        assert result.output == "page content"
        assert tool.execute_log == [{"input": "test"}]

    @pytest.mark.asyncio
    async def test_execute_nonexistent_tool(self, tool_router):
        result = await tool_router.execute_tool("nonexistent", {})
        assert result.status == ToolStatus.ERROR
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_execute_invalid_arguments(self, tool_router):
        tool = FakeTool(tool_name="web")
        tool_router.register_tool(tool)

        with patch.object(tool_router, "_is_tool_allowed", return_value=(True, None)):
            result = await tool_router.execute_tool("web", {})  # missing required 'input'

        assert result.status == ToolStatus.ERROR
        assert "Missing required" in result.error

    @pytest.mark.asyncio
    async def test_execute_tool_that_raises(self, tool_router):
        tool = FakeTool(tool_name="web", raise_on_execute=RuntimeError("kaboom"))
        tool_router.register_tool(tool)

        with patch.object(tool_router, "_is_tool_allowed", return_value=(True, None)):
            result = await tool_router.execute_tool("web", {"input": "x"})

        assert result.status == ToolStatus.ERROR
        assert "kaboom" in result.error

    @pytest.mark.asyncio
    async def test_execute_timeout(self, tool_router):
        tool = FakeTool(tool_name="web", delay_seconds=5.0)
        tool_router.register_tool(tool)

        with patch.object(tool_router, "_is_tool_allowed", return_value=(True, None)):
            result = await tool_router.execute_tool(
                "web", {"input": "x"}, timeout_seconds=0.05,
            )

        assert result.status == ToolStatus.ERROR
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_duration_ms_calculated(self, tool_router):
        tool = FakeTool(tool_name="web")
        tool_router.register_tool(tool)

        with patch.object(tool_router, "_is_tool_allowed", return_value=(True, None)):
            result = await tool_router.execute_tool("web", {"input": "x"})

        assert result.duration_ms is not None
        assert result.duration_ms >= 0


# ---------------------------------------------------------------------------
# Policy enforcement
# ---------------------------------------------------------------------------

class TestToolPolicy:
    def test_denied_tool(self):
        router = ToolRouter()
        with patch("aria.tools.router.settings") as mock_settings:
            mock_settings.tool_execution_policy = "open"
            mock_settings.tool_allowed_names = []
            mock_settings.tool_denied_names = ["bad_tool"]
            mock_settings.tool_sensitive_names = []
            allowed, reason = router._is_tool_allowed(tool_name="bad_tool", allow_sensitive=True)
        assert allowed is False
        assert "denied" in reason

    def test_allowlist_policy_blocks_unlisted(self):
        router = ToolRouter()
        with patch("aria.tools.router.settings") as mock_settings:
            mock_settings.tool_execution_policy = "allowlist"
            mock_settings.tool_allowed_names = ["web"]
            mock_settings.tool_denied_names = []
            mock_settings.tool_sensitive_names = []
            allowed, reason = router._is_tool_allowed(tool_name="shell", allow_sensitive=True)
        assert allowed is False
        assert "not in the tool allowlist" in reason

    def test_allowlist_allows_listed_tool(self):
        router = ToolRouter()
        with patch("aria.tools.router.settings") as mock_settings:
            mock_settings.tool_execution_policy = "allowlist"
            mock_settings.tool_allowed_names = ["web"]
            mock_settings.tool_denied_names = []
            mock_settings.tool_sensitive_names = []
            allowed, _ = router._is_tool_allowed(tool_name="web", allow_sensitive=True)
        assert allowed is True

    def test_sensitive_tool_blocked_when_not_allowed(self):
        router = ToolRouter()
        with patch("aria.tools.router.settings") as mock_settings:
            mock_settings.tool_execution_policy = "open"
            mock_settings.tool_allowed_names = []
            mock_settings.tool_denied_names = []
            mock_settings.tool_sensitive_names = ["shell"]
            allowed, reason = router._is_tool_allowed(tool_name="shell", allow_sensitive=False)
        assert allowed is False
        assert "restricted" in reason

    def test_sensitive_tool_allowed_when_flag_set(self):
        router = ToolRouter()
        with patch("aria.tools.router.settings") as mock_settings:
            mock_settings.tool_execution_policy = "open"
            mock_settings.tool_allowed_names = []
            mock_settings.tool_denied_names = []
            mock_settings.tool_sensitive_names = ["shell"]
            allowed, _ = router._is_tool_allowed(tool_name="shell", allow_sensitive=True)
        assert allowed is True


# ---------------------------------------------------------------------------
# Audit hook
# ---------------------------------------------------------------------------

class TestAuditHook:
    @pytest.mark.asyncio
    async def test_audit_hook_called(self, tool_router):
        audit_log = []

        async def hook(**kwargs):
            audit_log.append(kwargs)

        tool_router.set_audit_hook(hook)
        tool = FakeTool(tool_name="web")
        tool_router.register_tool(tool)

        with patch.object(tool_router, "_is_tool_allowed", return_value=(True, None)):
            await tool_router.execute_tool("web", {"input": "x"})

        assert len(audit_log) == 1
        assert audit_log[0]["category"] == "tool_execution"
        assert audit_log[0]["action"] == "web"
