"""
ARIA - MCP Client

Phase: 3
Purpose: Client implementation for Model Context Protocol (MCP)

Related Spec Sections:
- Section 8.3: Phase 3 - Tools & MCP

MCP Protocol:
- JSON-RPC 2.0 based communication
- Supports stdio and SSE transports
- Tools, prompts, and resources

Reference: https://modelcontextprotocol.io/
"""

import asyncio
import json
from typing import Any, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class MCPServerInfo:
    """Information about an MCP server."""
    name: str
    version: str
    protocol_version: str = "2024-11-05"
    capabilities: dict = None


@dataclass
class MCPTool:
    """Tool definition from MCP server."""
    name: str
    description: str
    input_schema: dict  # JSON Schema


class MCPClient:
    """
    Client for communicating with MCP servers via stdio.

    Implements JSON-RPC 2.0 protocol over stdin/stdout.
    """

    def __init__(self, command: list[str], env: Optional[dict] = None):
        """
        Initialize MCP client.

        Args:
            command: Command to start the MCP server (e.g., ["python", "server.py"])
            env: Environment variables for the server process
        """
        self.command = command
        self.env = env
        self.process: Optional[asyncio.subprocess.Process] = None
        self.server_info: Optional[MCPServerInfo] = None
        self.tools: dict[str, MCPTool] = {}
        self._request_id = 0
        self._connected = False

        logger.info(f"Initialized MCP client for command: {' '.join(command)}")

    async def connect(self) -> bool:
        """
        Connect to the MCP server by starting the process.

        Returns:
            True if connected successfully
        """
        try:
            logger.info(f"Starting MCP server: {' '.join(self.command)}")

            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )

            # Initialize the connection
            init_result = await self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                },
                "clientInfo": {
                    "name": "ARIA",
                    "version": "0.2.0",
                },
            })

            if not init_result:
                logger.error("Failed to initialize MCP connection")
                await self.disconnect()
                return False

            # Extract server info
            self.server_info = MCPServerInfo(
                name=init_result.get("serverInfo", {}).get("name", "Unknown"),
                version=init_result.get("serverInfo", {}).get("version", "0.0.0"),
                protocol_version=init_result.get("protocolVersion", "2024-11-05"),
                capabilities=init_result.get("capabilities", {}),
            )

            # Send initialized notification
            await self._send_notification("notifications/initialized")

            # List available tools
            await self._refresh_tools()

            self._connected = True
            logger.info(
                f"Connected to MCP server: {self.server_info.name} "
                f"v{self.server_info.version} ({len(self.tools)} tools)"
            )

            return True

        except Exception as e:
            logger.error(f"Failed to connect to MCP server: {str(e)}", exc_info=True)
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
            except Exception as e:
                logger.error(f"Error disconnecting from MCP server: {str(e)}")

        self.process = None
        self._connected = False
        logger.info("Disconnected from MCP server")

    @property
    def is_connected(self) -> bool:
        """Check if connected to the server."""
        return self._connected and self.process is not None

    async def _refresh_tools(self) -> None:
        """Refresh the list of available tools from the server."""
        result = await self._send_request("tools/list", {})

        if not result or "tools" not in result:
            logger.warning("Failed to list tools from MCP server")
            return

        self.tools.clear()
        for tool_data in result["tools"]:
            tool = MCPTool(
                name=tool_data["name"],
                description=tool_data.get("description", ""),
                input_schema=tool_data.get("inputSchema", {}),
            )
            self.tools[tool.name] = tool

        logger.info(f"Refreshed {len(self.tools)} tools from MCP server")

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """
        Call a tool on the MCP server.

        Args:
            tool_name: Name of the tool to call
            arguments: Arguments to pass to the tool

        Returns:
            Tool result from the server
        """
        if not self.is_connected:
            raise RuntimeError("Not connected to MCP server")

        if tool_name not in self.tools:
            raise ValueError(f"Tool '{tool_name}' not found on MCP server")

        result = await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

        return result

    async def _send_request(self, method: str, params: dict) -> Optional[dict]:
        """
        Send a JSON-RPC request and wait for response.

        Args:
            method: RPC method name
            params: Method parameters

        Returns:
            Response result or None on error
        """
        if not self.process or not self.process.stdin:
            raise RuntimeError("Process not started")

        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }

        # Send request
        request_json = json.dumps(request) + "\n"
        self.process.stdin.write(request_json.encode("utf-8"))
        await self.process.stdin.drain()

        # Read response
        try:
            response_line = await asyncio.wait_for(
                self.process.stdout.readline(),
                timeout=30,
            )

            if not response_line:
                logger.error("MCP server closed connection")
                return None

            response = json.loads(response_line.decode("utf-8"))

            if "error" in response:
                logger.error(f"MCP error: {response['error']}")
                return None

            return response.get("result")

        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for response to {method}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode MCP response: {str(e)}")
            return None

    async def _send_notification(self, method: str, params: dict = None) -> None:
        """
        Send a JSON-RPC notification (no response expected).

        Args:
            method: Notification method name
            params: Method parameters (optional)
        """
        if not self.process or not self.process.stdin:
            raise RuntimeError("Process not started")

        notification = {
            "jsonrpc": "2.0",
            "method": method,
        }

        if params:
            notification["params"] = params

        notification_json = json.dumps(notification) + "\n"
        self.process.stdin.write(notification_json.encode("utf-8"))
        await self.process.stdin.drain()

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.disconnect()
