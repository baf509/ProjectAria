"""
ARIA - MCP Manager

Phase: 3
Purpose: Manage MCP server lifecycle and provide MCP tools to the tool router

Related Spec Sections:
- Section 8.3: Phase 3 - Tools & MCP
"""

from typing import Optional
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorDatabase
from .client import MCPClient, MCPTool as MCPToolDef
from ..base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType
import logging

logger = logging.getLogger(__name__)


class MCPToolWrapper(BaseTool):
    """
    Wrapper that adapts an MCP tool to the BaseTool interface.
    """

    def __init__(self, mcp_client: MCPClient, mcp_tool: MCPToolDef):
        """
        Initialize MCP tool wrapper.

        Args:
            mcp_client: The MCP client managing this tool
            mcp_tool: The MCP tool definition
        """
        super().__init__()
        self.mcp_client = mcp_client
        self.mcp_tool = mcp_tool
        self._parameters = self._parse_parameters()

    @property
    def name(self) -> str:
        return self.mcp_tool.name

    @property
    def description(self) -> str:
        return self.mcp_tool.description

    @property
    def type(self) -> ToolType:
        return ToolType.MCP

    @property
    def parameters(self) -> list[ToolParameter]:
        return self._parameters

    def _parse_parameters(self) -> list[ToolParameter]:
        """Parse MCP input schema to ToolParameter list."""
        params = []
        schema = self.mcp_tool.input_schema

        if schema.get("type") != "object":
            return params

        properties = schema.get("properties", {})
        required = set(schema.get("required", []))

        for name, prop in properties.items():
            param = ToolParameter(
                name=name,
                type=prop.get("type", "string"),
                description=prop.get("description", ""),
                required=name in required,
                default=prop.get("default"),
                enum=prop.get("enum"),
                items=prop.get("items"),
                properties=prop.get("properties"),
            )
            params.append(param)

        return params

    async def execute(self, arguments: dict) -> ToolResult:
        """Execute the MCP tool."""
        started_at = datetime.now(timezone.utc)

        try:
            # Call the tool via MCP client
            result = await self.mcp_client.call_tool(self.mcp_tool.name, arguments)

            completed_at = datetime.now(timezone.utc)
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            if result is None:
                return ToolResult(
                    tool_name=self.name,
                    status=ToolStatus.ERROR,
                    error="MCP tool returned no result",
                    duration_ms=duration_ms,
                    started_at=started_at,
                    completed_at=completed_at,
                )

            # MCP tools return content array
            content = result.get("content", [])

            # Combine text content
            output_parts = []
            for item in content:
                if item.get("type") == "text":
                    output_parts.append(item.get("text", ""))

            output = "\n".join(output_parts) if output_parts else result

            # Check if there's an error
            is_error = result.get("isError", False)
            status = ToolStatus.ERROR if is_error else ToolStatus.SUCCESS

            return ToolResult(
                tool_name=self.name,
                status=status,
                output=output,
                error=None if not is_error else "MCP tool reported error",
                duration_ms=duration_ms,
                started_at=started_at,
                completed_at=completed_at,
                metadata={"mcp_result": result},
            )

        except Exception as e:
            completed_at = datetime.now(timezone.utc)
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            logger.error(f"MCP tool {self.name} failed: {str(e)}", exc_info=True)

            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"MCP tool execution failed: {str(e)}",
                duration_ms=duration_ms,
                started_at=started_at,
                completed_at=completed_at,
            )


class MCPManager:
    """
    Manages MCP server instances and their tools.
    """

    def __init__(self):
        self.servers: dict[str, MCPClient] = {}
        logger.info("Initialized MCP manager")

    async def add_server(
        self,
        server_id: str,
        command: list[str],
        env: Optional[dict] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Add and connect to an MCP server.

        Args:
            server_id: Unique identifier for this server
            command: Command to start the server
            env: Environment variables for the server

        Returns:
            (success, error_message)
        """
        if server_id in self.servers:
            return False, f"Server '{server_id}' already exists"

        try:
            client = MCPClient(command, env)
            connected = await client.connect()

            if not connected:
                return False, "Failed to connect to MCP server"

            self.servers[server_id] = client
            logger.info(f"Added MCP server: {server_id}")

            return True, None

        except Exception as e:
            logger.error(f"Failed to add MCP server {server_id}: {str(e)}", exc_info=True)
            return False, str(e)

    async def remove_server(self, server_id: str) -> bool:
        """
        Remove and disconnect from an MCP server.

        Args:
            server_id: ID of the server to remove

        Returns:
            True if server was removed
        """
        if server_id not in self.servers:
            return False

        client = self.servers[server_id]
        await client.disconnect()
        del self.servers[server_id]

        logger.info(f"Removed MCP server: {server_id}")
        return True

    def get_server(self, server_id: str) -> Optional[MCPClient]:
        """Get an MCP client by server ID."""
        return self.servers.get(server_id)

    def list_servers(self) -> list[dict]:
        """
        List all registered MCP servers.

        Returns:
            List of server info dictionaries
        """
        servers = []
        for server_id, client in self.servers.items():
            info = {
                "id": server_id,
                "connected": client.is_connected,
                "command": " ".join(client.command),
                "tool_count": len(client.tools),
            }

            if client.server_info:
                info["name"] = client.server_info.name
                info["version"] = client.server_info.version

            servers.append(info)

        return servers

    def get_all_tools(self) -> list[BaseTool]:
        """
        Get all tools from all MCP servers as BaseTool instances.

        Returns:
            List of MCPToolWrapper instances
        """
        tools = []

        for server_id, client in self.servers.items():
            if not client.is_connected:
                continue

            for tool_name, mcp_tool in client.tools.items():
                wrapper = MCPToolWrapper(client, mcp_tool)
                tools.append(wrapper)

        return tools

    def get_server_tools(self, server_id: str) -> list[BaseTool]:
        """
        Get all tools from a specific MCP server.

        Args:
            server_id: ID of the server

        Returns:
            List of MCPToolWrapper instances
        """
        client = self.servers.get(server_id)
        if not client or not client.is_connected:
            return []

        tools = []
        for tool_name, mcp_tool in client.tools.items():
            wrapper = MCPToolWrapper(client, mcp_tool)
            tools.append(wrapper)

        return tools

    async def save_server_config(
        self,
        db: AsyncIOMotorDatabase,
        server_id: str,
        command: list[str],
        env: Optional[dict] = None,
    ) -> None:
        """
        Persist an MCP server config to MongoDB.

        Uses upsert so re-adding a server updates the existing record.
        """
        now = datetime.now(timezone.utc)
        await db.mcp_servers.update_one(
            {"server_id": server_id},
            {
                "$set": {
                    "server_id": server_id,
                    "command": command,
                    "args": [],
                    "env": env or {},
                    "enabled": True,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "created_at": now,
                },
            },
            upsert=True,
        )
        logger.info(f"Saved MCP server config: {server_id}")

    async def load_saved_servers(self, db: AsyncIOMotorDatabase) -> int:
        """
        Load all enabled MCP server configs from MongoDB and start them.

        Returns:
            Number of servers successfully started.
        """
        started = 0
        cursor = db.mcp_servers.find({"enabled": True})

        async for doc in cursor:
            server_id = doc["server_id"]
            command = doc["command"]
            env = doc.get("env") or None

            if server_id in self.servers:
                logger.debug(f"MCP server already loaded: {server_id}")
                started += 1
                continue

            success, error = await self.add_server(
                server_id=server_id,
                command=command,
                env=env,
            )

            if success:
                started += 1
                logger.info(f"Restored MCP server from DB: {server_id}")
            else:
                logger.warning(
                    f"Failed to restore MCP server {server_id}: {error}"
                )

        logger.info(f"Loaded {started} saved MCP server(s) from database")
        return started

    async def delete_server_config(
        self, db: AsyncIOMotorDatabase, server_id: str
    ) -> bool:
        """
        Remove an MCP server config from MongoDB.

        Returns:
            True if a document was deleted.
        """
        result = await db.mcp_servers.delete_one({"server_id": server_id})
        deleted = result.deleted_count > 0
        if deleted:
            logger.info(f"Deleted MCP server config: {server_id}")
        return deleted

    async def shutdown_all(self) -> None:
        """Disconnect from all MCP servers."""
        for server_id in list(self.servers.keys()):
            await self.remove_server(server_id)

        logger.info("Shut down all MCP servers")
