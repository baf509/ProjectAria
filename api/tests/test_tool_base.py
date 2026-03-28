"""Tests for aria.tools.base — ToolDefinition, ToolResult, and BaseTool."""

import pytest

from aria.tools.base import (
    ToolDefinition,
    ToolParameter,
    ToolResult,
    ToolStatus,
)

from tests.conftest import FakeTool


# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

class TestToolDefinition:
    def test_to_json_schema_basic(self):
        td = ToolDefinition(
            name="test",
            description="A test tool",
            parameters=[
                ToolParameter(name="url", type="string", description="URL to fetch", required=True),
                ToolParameter(name="timeout", type="number", description="Timeout", required=False, default=30),
            ],
        )
        schema = td.to_json_schema()
        assert schema["type"] == "object"
        assert "url" in schema["properties"]
        assert "timeout" in schema["properties"]
        assert "url" in schema["required"]
        assert "timeout" not in schema["required"]
        assert schema["properties"]["timeout"]["default"] == 30

    def test_to_llm_tool(self):
        td = ToolDefinition(
            name="web",
            description="Fetch a page",
            parameters=[],
        )
        tool = td.to_llm_tool()
        assert tool["name"] == "web"
        assert tool["description"] == "Fetch a page"
        assert tool["parameters"]["type"] == "object"

    def test_enum_parameter(self):
        td = ToolDefinition(
            name="test",
            description="test",
            parameters=[
                ToolParameter(name="mode", type="string", description="Mode", enum=["a", "b"]),
            ],
        )
        schema = td.to_json_schema()
        assert schema["properties"]["mode"]["enum"] == ["a", "b"]


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------

class TestToolResult:
    def test_is_success(self):
        r = ToolResult(tool_name="t", status=ToolStatus.SUCCESS, output="ok")
        assert r.is_success() is True
        assert r.is_error() is False

    def test_is_error(self):
        r = ToolResult(tool_name="t", status=ToolStatus.ERROR, error="fail")
        assert r.is_error() is True
        assert r.is_success() is False


# ---------------------------------------------------------------------------
# BaseTool argument validation
# ---------------------------------------------------------------------------

class TestBaseToolValidation:
    @pytest.mark.asyncio
    async def test_valid_arguments(self):
        tool = FakeTool()
        is_valid, error = await tool.validate_arguments({"input": "hello"})
        assert is_valid is True
        assert error is None

    @pytest.mark.asyncio
    async def test_missing_required(self):
        tool = FakeTool()
        is_valid, error = await tool.validate_arguments({})
        assert is_valid is False
        assert "Missing required" in error

    @pytest.mark.asyncio
    async def test_unknown_parameter(self):
        tool = FakeTool()
        is_valid, error = await tool.validate_arguments({"input": "ok", "extra": "bad"})
        assert is_valid is False
        assert "Unknown parameter" in error
