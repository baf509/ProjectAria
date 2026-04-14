"""Tests for MCP client, tool wrapper, and manager.

Covers:
  1. MCPClient — JSON-RPC connect/disconnect, call_tool, error handling
  2. MCPToolWrapper — parameter parsing, execute success/error
  3. MCPManager — server lifecycle, tool aggregation
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.tools.base import ToolStatus, ToolType
from aria.tools.mcp.client import MCPClient, MCPServerInfo, MCPTool
from aria.tools.mcp.manager import MCPManager, MCPToolWrapper


# ============================================================================
# MCPClient
# ============================================================================

class TestMCPClient:
    """Tests for MCP JSON-RPC client."""

    def _make_fake_process(self, responses: list[dict]):
        """Create a fake subprocess that returns pre-built JSON-RPC responses."""
        process = AsyncMock()
        process.returncode = None  # still running
        process.stdin = MagicMock()
        process.stdin.write = MagicMock()
        process.stdin.drain = AsyncMock()
        process.stderr = AsyncMock()

        # Build line-by-line async readline responses
        response_lines = [
            (json.dumps(r) + "\n").encode("utf-8") for r in responses
        ]
        process.stdout = AsyncMock()
        process.stdout.readline = AsyncMock(side_effect=response_lines)

        process.terminate = MagicMock()
        process.kill = MagicMock()
        process.wait = AsyncMock()

        return process

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """Full connect handshake: initialize → initialized notification → tools/list."""
        init_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "test-server", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            },
        }
        tools_response = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {
                        "name": "brave_search",
                        "description": "Search the web via Brave",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Search query"},
                            },
                            "required": ["query"],
                        },
                    },
                ],
            },
        }

        process = self._make_fake_process([init_response, tools_response])

        with patch("aria.tools.mcp.client.asyncio.create_subprocess_exec",
                    return_value=process):
            client = MCPClient(["python", "server.py"])
            connected = await client.connect()

        assert connected is True
        assert client.is_connected is True
        assert client.server_info.name == "test-server"
        assert "brave_search" in client.tools
        assert client.tools["brave_search"].description == "Search the web via Brave"

    @pytest.mark.asyncio
    async def test_connect_failure(self):
        """Initialize fails → returns False, client disconnected."""
        error_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32600, "message": "Invalid request"},
        }
        process = self._make_fake_process([error_response])

        with patch("aria.tools.mcp.client.asyncio.create_subprocess_exec",
                    return_value=process):
            client = MCPClient(["python", "server.py"])
            connected = await client.connect()

        assert connected is False
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        """call_tool sends tools/call and returns result."""
        # Set up a connected client
        init_resp = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "s", "version": "1"},
                "capabilities": {},
            },
        }
        tools_resp = {
            "jsonrpc": "2.0", "id": 2,
            "result": {
                "tools": [{
                    "name": "web_search",
                    "description": "Search",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }],
            },
        }
        tool_call_resp = {
            "jsonrpc": "2.0", "id": 3,
            "result": {
                "content": [{"type": "text", "text": "Result: found 5 items"}],
                "isError": False,
            },
        }

        process = self._make_fake_process([init_resp, tools_resp, tool_call_resp])

        with patch("aria.tools.mcp.client.asyncio.create_subprocess_exec",
                    return_value=process):
            client = MCPClient(["node", "server.js"])
            await client.connect()
            result = await client.call_tool("web_search", {"query": "ARIA AI"})

        assert result["content"][0]["text"] == "Result: found 5 items"
        assert result["isError"] is False

    @pytest.mark.asyncio
    async def test_call_tool_not_connected(self):
        client = MCPClient(["python", "server.py"])
        with pytest.raises(RuntimeError, match="Not connected"):
            await client.call_tool("anything", {})

    @pytest.mark.asyncio
    async def test_call_tool_unknown(self):
        """Calling a tool that doesn't exist on the server."""
        init_resp = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "s", "version": "1"},
                "capabilities": {},
            },
        }
        tools_resp = {
            "jsonrpc": "2.0", "id": 2,
            "result": {"tools": []},
        }

        process = self._make_fake_process([init_resp, tools_resp])

        with patch("aria.tools.mcp.client.asyncio.create_subprocess_exec",
                    return_value=process):
            client = MCPClient(["python", "server.py"])
            await client.connect()

            with pytest.raises(ValueError, match="not found"):
                await client.call_tool("nonexistent", {})

    @pytest.mark.asyncio
    async def test_disconnect(self):
        init_resp = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "s", "version": "1"},
                "capabilities": {},
            },
        }
        tools_resp = {
            "jsonrpc": "2.0", "id": 2,
            "result": {"tools": []},
        }

        process = self._make_fake_process([init_resp, tools_resp])

        with patch("aria.tools.mcp.client.asyncio.create_subprocess_exec",
                    return_value=process):
            client = MCPClient(["python", "server.py"])
            await client.connect()
            assert client.is_connected

            await client.disconnect()
            assert client.is_connected is False
            assert client.process is None

    @pytest.mark.asyncio
    async def test_context_manager(self):
        init_resp = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "s", "version": "1"},
                "capabilities": {},
            },
        }
        tools_resp = {
            "jsonrpc": "2.0", "id": 2,
            "result": {"tools": []},
        }

        process = self._make_fake_process([init_resp, tools_resp])

        with patch("aria.tools.mcp.client.asyncio.create_subprocess_exec",
                    return_value=process):
            async with MCPClient(["python", "server.py"]) as client:
                assert client.is_connected
            # After exit, should be disconnected
            assert client.is_connected is False


# ============================================================================
# MCPToolWrapper
# ============================================================================

class TestMCPToolWrapper:
    """Tests for the BaseTool wrapper around MCP tools."""

    def _make_wrapper(self, input_schema=None, tool_name="brave_search"):
        if input_schema is None:
            input_schema = {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "count": {"type": "number", "description": "Results", "default": 10},
                },
                "required": ["query"],
            }

        client = MagicMock(spec=MCPClient)
        client.is_connected = True
        mcp_tool = MCPTool(
            name=tool_name,
            description="Search the web",
            input_schema=input_schema,
        )
        return MCPToolWrapper(client, mcp_tool)

    # -- Properties --

    def test_properties(self):
        wrapper = self._make_wrapper()
        assert wrapper.name == "brave_search"
        assert wrapper.type == ToolType.MCP
        assert wrapper.description == "Search the web"

    # -- Parameter parsing --

    def test_parse_parameters(self):
        wrapper = self._make_wrapper()
        params = wrapper.parameters
        names = {p.name for p in params}
        assert names == {"query", "count"}

        query_param = next(p for p in params if p.name == "query")
        assert query_param.required is True

        count_param = next(p for p in params if p.name == "count")
        assert count_param.required is False
        assert count_param.default == 10

    def test_parse_empty_schema(self):
        wrapper = self._make_wrapper(input_schema={"type": "string"})
        assert wrapper.parameters == []

    def test_parse_enum_parameters(self):
        wrapper = self._make_wrapper(input_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "description": "Mode", "enum": ["fast", "deep"]},
            },
            "required": [],
        })
        mode = wrapper.parameters[0]
        assert mode.enum == ["fast", "deep"]

    # -- Execute success --

    @pytest.mark.asyncio
    async def test_execute_success(self):
        wrapper = self._make_wrapper()
        wrapper.mcp_client.call_tool = AsyncMock(return_value={
            "content": [
                {"type": "text", "text": "Result 1: ARIA is an AI agent platform"},
                {"type": "text", "text": "Result 2: More info"},
            ],
            "isError": False,
        })

        result = await wrapper.execute({"query": "ARIA AI"})
        assert result.status == ToolStatus.SUCCESS
        assert "ARIA" in result.output
        assert "Result 2" in result.output
        wrapper.mcp_client.call_tool.assert_called_once_with("brave_search", {"query": "ARIA AI"})

    # -- Execute error from MCP server --

    @pytest.mark.asyncio
    async def test_execute_mcp_error(self):
        wrapper = self._make_wrapper()
        wrapper.mcp_client.call_tool = AsyncMock(return_value={
            "content": [{"type": "text", "text": "Rate limited"}],
            "isError": True,
        })

        result = await wrapper.execute({"query": "test"})
        assert result.status == ToolStatus.ERROR

    # -- Execute returns None --

    @pytest.mark.asyncio
    async def test_execute_null_result(self):
        wrapper = self._make_wrapper()
        wrapper.mcp_client.call_tool = AsyncMock(return_value=None)

        result = await wrapper.execute({"query": "test"})
        assert result.status == ToolStatus.ERROR
        assert "no result" in result.error.lower()

    # -- Execute raises exception --

    @pytest.mark.asyncio
    async def test_execute_exception(self):
        wrapper = self._make_wrapper()
        wrapper.mcp_client.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))

        result = await wrapper.execute({"query": "test"})
        assert result.status == ToolStatus.ERROR
        assert "connection lost" in result.error

    # -- Duration tracking --

    @pytest.mark.asyncio
    async def test_duration_tracked(self):
        wrapper = self._make_wrapper()
        wrapper.mcp_client.call_tool = AsyncMock(return_value={
            "content": [{"type": "text", "text": "ok"}],
            "isError": False,
        })

        result = await wrapper.execute({"query": "test"})
        assert result.duration_ms is not None
        assert result.duration_ms >= 0
        assert result.started_at is not None
        assert result.completed_at is not None


# ============================================================================
# MCPManager
# ============================================================================

class TestMCPManager:
    """Tests for MCP server lifecycle management."""

    @pytest.mark.asyncio
    async def test_add_server_success(self):
        manager = MCPManager()

        with patch.object(MCPClient, "connect", return_value=True):
            success, error = await manager.add_server(
                "brave", ["npx", "-y", "@anthropic/brave-mcp"]
            )

        assert success is True
        assert error is None
        assert "brave" in manager.servers

    @pytest.mark.asyncio
    async def test_add_server_duplicate(self):
        manager = MCPManager()

        with patch.object(MCPClient, "connect", return_value=True):
            await manager.add_server("brave", ["npx", "brave"])
            success, error = await manager.add_server("brave", ["npx", "brave"])

        assert success is False
        assert "already exists" in error

    @pytest.mark.asyncio
    async def test_add_server_connect_failure(self):
        manager = MCPManager()

        with patch.object(MCPClient, "connect", return_value=False):
            success, error = await manager.add_server("bad", ["bad-server"])

        assert success is False
        assert "Failed" in error

    @pytest.mark.asyncio
    async def test_remove_server(self):
        manager = MCPManager()

        with patch.object(MCPClient, "connect", return_value=True):
            await manager.add_server("brave", ["npx", "brave"])

        with patch.object(MCPClient, "disconnect", return_value=None):
            removed = await manager.remove_server("brave")

        assert removed is True
        assert "brave" not in manager.servers

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self):
        manager = MCPManager()
        removed = await manager.remove_server("nope")
        assert removed is False

    @pytest.mark.asyncio
    async def test_list_servers(self):
        manager = MCPManager()

        client = MagicMock(spec=MCPClient)
        client.is_connected = True
        client.command = ["npx", "brave"]
        client.tools = {"search": MagicMock()}
        client.server_info = MCPServerInfo(name="Brave", version="1.0")
        manager.servers["brave"] = client

        servers = manager.list_servers()
        assert len(servers) == 1
        assert servers[0]["id"] == "brave"
        assert servers[0]["connected"] is True
        assert servers[0]["tool_count"] == 1
        assert servers[0]["name"] == "Brave"

    def test_get_all_tools(self):
        manager = MCPManager()

        client = MagicMock(spec=MCPClient)
        client.is_connected = True
        client.tools = {
            "search": MCPTool(name="search", description="Search", input_schema={"type": "object", "properties": {}}),
            "summarize": MCPTool(name="summarize", description="Summarize", input_schema={"type": "object", "properties": {}}),
        }
        manager.servers["brave"] = client

        tools = manager.get_all_tools()
        assert len(tools) == 2
        assert all(isinstance(t, MCPToolWrapper) for t in tools)
        names = {t.name for t in tools}
        assert names == {"search", "summarize"}

    def test_get_all_tools_skips_disconnected(self):
        manager = MCPManager()

        client = MagicMock(spec=MCPClient)
        client.is_connected = False
        client.tools = {"search": MCPTool(name="search", description="S", input_schema={})}
        manager.servers["dead"] = client

        assert manager.get_all_tools() == []

    @pytest.mark.asyncio
    async def test_shutdown_all(self):
        manager = MCPManager()

        clients = []
        for name in ["a", "b"]:
            c = MagicMock(spec=MCPClient)
            c.disconnect = AsyncMock()
            manager.servers[name] = c
            clients.append(c)

        await manager.shutdown_all()

        assert len(manager.servers) == 0
        for c in clients:
            c.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_and_load_server_config(self):
        """Test persistence to/from MongoDB."""
        manager = MCPManager()

        mock_db = MagicMock()
        mock_db.mcp_servers = MagicMock()
        mock_db.mcp_servers.update_one = AsyncMock()

        await manager.save_server_config(
            mock_db, "brave", ["npx", "brave"], env={"API_KEY": "xxx"}
        )

        mock_db.mcp_servers.update_one.assert_called_once()
        call_args = mock_db.mcp_servers.update_one.call_args
        assert call_args[0][0] == {"server_id": "brave"}

    @pytest.mark.asyncio
    async def test_delete_server_config(self):
        manager = MCPManager()

        mock_db = MagicMock()
        mock_db.mcp_servers = MagicMock()
        mock_db.mcp_servers.delete_one = AsyncMock(
            return_value=MagicMock(deleted_count=1)
        )

        deleted = await manager.delete_server_config(mock_db, "brave")
        assert deleted is True
        mock_db.mcp_servers.delete_one.assert_called_once_with({"server_id": "brave"})
