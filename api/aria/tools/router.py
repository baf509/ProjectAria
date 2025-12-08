"""
ARIA - Tool Router

Phase: 3
Purpose: Route tool calls to appropriate handlers and manage tool registry

Related Spec Sections:
- Section 8.3: Phase 3 - Tools & MCP
"""

import asyncio
from typing import Optional
from datetime import datetime
from .base import BaseTool, ToolResult, ToolStatus, ToolType
import logging

logger = logging.getLogger(__name__)


class ToolRouter:
    """
    Routes tool calls to registered tools.
    Manages both built-in tools and MCP tools.
    """

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._builtin_tools: dict[str, BaseTool] = {}
        self._mcp_tools: dict[str, BaseTool] = {}

    def register_tool(self, tool: BaseTool) -> None:
        """
        Register a tool with the router.

        Args:
            tool: Tool instance to register

        Raises:
            ValueError: If a tool with the same name already exists
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")

        self._tools[tool.name] = tool

        # Track by type
        if tool.type == ToolType.BUILTIN:
            self._builtin_tools[tool.name] = tool
        elif tool.type == ToolType.MCP:
            self._mcp_tools[tool.name] = tool

        logger.info(f"Registered {tool.type} tool: {tool.name}")

    def unregister_tool(self, tool_name: str) -> bool:
        """
        Unregister a tool.

        Args:
            tool_name: Name of the tool to unregister

        Returns:
            True if tool was unregistered, False if not found
        """
        if tool_name not in self._tools:
            return False

        tool = self._tools[tool_name]

        # Remove from main registry
        del self._tools[tool_name]

        # Remove from type-specific registry
        if tool.type == ToolType.BUILTIN:
            self._builtin_tools.pop(tool_name, None)
        elif tool.type == ToolType.MCP:
            self._mcp_tools.pop(tool_name, None)

        logger.info(f"Unregistered tool: {tool_name}")
        return True

    def get_tool(self, tool_name: str) -> Optional[BaseTool]:
        """Get a tool by name."""
        return self._tools.get(tool_name)

    def list_tools(
        self,
        tool_type: Optional[ToolType] = None,
        enabled_only: bool = False,
    ) -> list[BaseTool]:
        """
        List all registered tools.

        Args:
            tool_type: Filter by tool type (optional)
            enabled_only: Only return enabled tools (for agent filtering)

        Returns:
            List of tool instances
        """
        if tool_type == ToolType.BUILTIN:
            tools = list(self._builtin_tools.values())
        elif tool_type == ToolType.MCP:
            tools = list(self._mcp_tools.values())
        else:
            tools = list(self._tools.values())

        return tools

    def get_tool_definitions(
        self,
        enabled_tools: Optional[list[str]] = None,
    ) -> list[dict]:
        """
        Get tool definitions for LLM consumption.

        Args:
            enabled_tools: List of tool names to include (None = all tools)

        Returns:
            List of tool definitions in LLM format
        """
        definitions = []

        for tool_name, tool in self._tools.items():
            # Filter by enabled tools if specified
            if enabled_tools is not None and tool_name not in enabled_tools:
                continue

            definitions.append(tool.definition.to_llm_tool())

        return definitions

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict,
        timeout_seconds: int = 300,
    ) -> ToolResult:
        """
        Execute a tool with the given arguments.

        Args:
            tool_name: Name of the tool to execute
            arguments: Arguments to pass to the tool
            timeout_seconds: Maximum execution time in seconds

        Returns:
            ToolResult with execution outcome
        """
        started_at = datetime.utcnow()

        # Check if tool exists
        tool = self.get_tool(tool_name)
        if not tool:
            return ToolResult(
                tool_name=tool_name,
                status=ToolStatus.ERROR,
                error=f"Tool '{tool_name}' not found",
                started_at=started_at,
                completed_at=datetime.utcnow(),
            )

        # Validate arguments
        is_valid, error_msg = await tool.validate_arguments(arguments)
        if not is_valid:
            return ToolResult(
                tool_name=tool_name,
                status=ToolStatus.ERROR,
                error=f"Invalid arguments: {error_msg}",
                started_at=started_at,
                completed_at=datetime.utcnow(),
            )

        # Execute with timeout
        try:
            logger.info(f"Executing tool: {tool_name} with args: {arguments}")

            result = await asyncio.wait_for(
                tool.execute(arguments),
                timeout=timeout_seconds,
            )

            # Set timing if not already set
            if result.started_at is None:
                result.started_at = started_at
            if result.completed_at is None:
                result.completed_at = datetime.utcnow()

            # Calculate duration if not set
            if result.duration_ms is None and result.started_at and result.completed_at:
                delta = result.completed_at - result.started_at
                result.duration_ms = int(delta.total_seconds() * 1000)

            logger.info(
                f"Tool {tool_name} completed with status: {result.status} "
                f"in {result.duration_ms}ms"
            )

            return result

        except asyncio.TimeoutError:
            completed_at = datetime.utcnow()
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            logger.error(f"Tool {tool_name} timed out after {timeout_seconds}s")

            return ToolResult(
                tool_name=tool_name,
                status=ToolStatus.ERROR,
                error=f"Tool execution timed out after {timeout_seconds} seconds",
                duration_ms=duration_ms,
                started_at=started_at,
                completed_at=completed_at,
            )

        except Exception as e:
            completed_at = datetime.utcnow()
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            logger.error(f"Tool {tool_name} failed with error: {str(e)}", exc_info=True)

            return ToolResult(
                tool_name=tool_name,
                status=ToolStatus.ERROR,
                error=f"Tool execution failed: {str(e)}",
                duration_ms=duration_ms,
                started_at=started_at,
                completed_at=completed_at,
            )

    def has_tool(self, tool_name: str) -> bool:
        """Check if a tool is registered."""
        return tool_name in self._tools

    def tool_count(self) -> dict[str, int]:
        """Get count of registered tools by type."""
        return {
            "total": len(self._tools),
            "builtin": len(self._builtin_tools),
            "mcp": len(self._mcp_tools),
        }

    def clear_tools(self, tool_type: Optional[ToolType] = None) -> int:
        """
        Clear all tools or tools of a specific type.

        Args:
            tool_type: Type of tools to clear (None = all)

        Returns:
            Number of tools cleared
        """
        if tool_type == ToolType.BUILTIN:
            count = len(self._builtin_tools)
            for name in list(self._builtin_tools.keys()):
                self.unregister_tool(name)
            return count

        elif tool_type == ToolType.MCP:
            count = len(self._mcp_tools)
            for name in list(self._mcp_tools.keys()):
                self.unregister_tool(name)
            return count

        else:
            count = len(self._tools)
            self._tools.clear()
            self._builtin_tools.clear()
            self._mcp_tools.clear()
            logger.info("Cleared all tools")
            return count
