"""
ARIA - Tools API Routes

Phase: 3
Purpose: API endpoints for tool management

Related Spec Sections:
- Section 5.1: REST Endpoints
- Section 8.3: Phase 3 - Tools & MCP
"""

from typing import Optional, Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aria.api.deps import get_tool_router, get_mcp_manager
from aria.tools.router import ToolRouter
from aria.tools.mcp.manager import MCPManager
from aria.tools.base import ToolType

router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class ToolExecuteRequest(BaseModel):
    """Request to execute a tool."""
    tool_name: str
    arguments: dict
    timeout: Optional[int] = None


class ToolExecuteResponse(BaseModel):
    """Response from tool execution."""
    tool_name: str
    status: str
    output: Any = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    metadata: dict = {}


class ToolDefinitionResponse(BaseModel):
    """Tool definition response."""
    name: str
    description: str
    type: str
    parameters: list[dict]


class MCPServerAddRequest(BaseModel):
    """Request to add an MCP server."""
    server_id: str
    command: list[str]
    env: Optional[dict] = None


class MCPServerResponse(BaseModel):
    """MCP server info response."""
    id: str
    connected: bool
    command: str
    tool_count: int
    name: Optional[str] = None
    version: Optional[str] = None


# =============================================================================
# Tool Endpoints
# =============================================================================

@router.get("/tools", response_model=list[ToolDefinitionResponse])
async def list_tools(
    tool_type: Optional[str] = None,
    router: ToolRouter = Depends(get_tool_router),
):
    """
    List all registered tools.

    Query parameters:
    - tool_type: Filter by type ("builtin" or "mcp")
    """
    # Filter by type if specified
    type_filter = None
    if tool_type:
        try:
            type_filter = ToolType(tool_type)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid tool_type. Must be 'builtin' or 'mcp'",
            )

    tools = router.list_tools(tool_type=type_filter)

    return [
        ToolDefinitionResponse(
            name=tool.name,
            description=tool.description,
            type=tool.type.value,
            parameters=[
                {
                    "name": p.name,
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                    "default": p.default,
                    "enum": p.enum,
                }
                for p in tool.parameters
            ],
        )
        for tool in tools
    ]


@router.get("/tools/{tool_name}", response_model=ToolDefinitionResponse)
async def get_tool(
    tool_name: str,
    router: ToolRouter = Depends(get_tool_router),
):
    """Get a specific tool by name."""
    tool = router.get_tool(tool_name)

    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    return ToolDefinitionResponse(
        name=tool.name,
        description=tool.description,
        type=tool.type.value,
        parameters=[
            {
                "name": p.name,
                "type": p.type,
                "description": p.description,
                "required": p.required,
                "default": p.default,
                "enum": p.enum,
            }
            for p in tool.parameters
        ],
    )


@router.post("/tools/execute", response_model=ToolExecuteResponse)
async def execute_tool(
    request: ToolExecuteRequest,
    router: ToolRouter = Depends(get_tool_router),
):
    """Execute a tool with the given arguments."""
    result = await router.execute_tool(
        tool_name=request.tool_name,
        arguments=request.arguments,
        timeout_seconds=request.timeout or 300,
    )

    return ToolExecuteResponse(
        tool_name=result.tool_name,
        status=result.status.value,
        output=result.output,
        error=result.error,
        duration_ms=result.duration_ms,
        metadata=result.metadata,
    )


# =============================================================================
# MCP Server Endpoints
# =============================================================================

@router.get("/mcp/servers", response_model=list[MCPServerResponse])
async def list_mcp_servers(
    mcp_manager: MCPManager = Depends(get_mcp_manager),
):
    """List all registered MCP servers."""
    servers = mcp_manager.list_servers()

    return [
        MCPServerResponse(
            id=server["id"],
            connected=server["connected"],
            command=server["command"],
            tool_count=server["tool_count"],
            name=server.get("name"),
            version=server.get("version"),
        )
        for server in servers
    ]


@router.post("/mcp/servers", status_code=201)
async def add_mcp_server(
    request: MCPServerAddRequest,
    router: ToolRouter = Depends(get_tool_router),
    mcp_manager: MCPManager = Depends(get_mcp_manager),
):
    """Add and connect to an MCP server."""
    success, error = await mcp_manager.add_server(
        server_id=request.server_id,
        command=request.command,
        env=request.env,
    )

    if not success:
        raise HTTPException(status_code=400, detail=error)

    # Register all tools from the new server
    tools = mcp_manager.get_server_tools(request.server_id)
    for tool in tools:
        try:
            router.register_tool(tool)
        except ValueError as e:
            # Tool already exists - this is ok
            pass

    return {
        "message": f"MCP server '{request.server_id}' added successfully",
        "tool_count": len(tools),
    }


@router.delete("/mcp/servers/{server_id}", status_code=204)
async def remove_mcp_server(
    server_id: str,
    router: ToolRouter = Depends(get_tool_router),
    mcp_manager: MCPManager = Depends(get_mcp_manager),
):
    """Remove and disconnect from an MCP server."""
    # Get tools before removing to unregister them
    tools = mcp_manager.get_server_tools(server_id)

    success = await mcp_manager.remove_server(server_id)

    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' not found",
        )

    # Unregister all tools from this server
    for tool in tools:
        router.unregister_tool(tool.name)

    return None


@router.get("/mcp/servers/{server_id}/tools", response_model=list[ToolDefinitionResponse])
async def list_mcp_server_tools(
    server_id: str,
    mcp_manager: MCPManager = Depends(get_mcp_manager),
):
    """List all tools provided by a specific MCP server."""
    tools = mcp_manager.get_server_tools(server_id)

    if not tools and server_id not in [s["id"] for s in mcp_manager.list_servers()]:
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' not found",
        )

    return [
        ToolDefinitionResponse(
            name=tool.name,
            description=tool.description,
            type=tool.type.value,
            parameters=[
                {
                    "name": p.name,
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                    "default": p.default,
                    "enum": p.enum,
                }
                for p in tool.parameters
            ],
        )
        for tool in tools
    ]


# =============================================================================
# Stats Endpoint
# =============================================================================

@router.get("/tools/stats")
async def get_tool_stats(
    router: ToolRouter = Depends(get_tool_router),
):
    """Get statistics about registered tools."""
    return router.tool_count()
