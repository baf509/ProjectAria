"""
ARIA - API Dependencies

Phase: 1, 3
Purpose: Dependency injection for FastAPI routes

Related Spec Sections:
- Section 9.4: Dependency Injection
"""

from typing import Annotated
from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorDatabase
from aria.db.mongodb import get_database
from aria.core.orchestrator import Orchestrator
from aria.tools.router import ToolRouter
from aria.tools.mcp.manager import MCPManager

# Global instances
_tool_router: ToolRouter = None
_mcp_manager: MCPManager = None


def get_tool_router() -> ToolRouter:
    """Get tool router instance."""
    global _tool_router
    if _tool_router is None:
        _tool_router = ToolRouter()
    return _tool_router


def get_mcp_manager() -> MCPManager:
    """Get MCP manager instance."""
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPManager()
    return _mcp_manager


async def get_db() -> AsyncIOMotorDatabase:
    """Get database instance."""
    return await get_database()


async def get_orchestrator(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    tool_router: Annotated[ToolRouter, Depends(get_tool_router)],
) -> Orchestrator:
    """Get orchestrator instance."""
    return Orchestrator(db, tool_router)
