"""
ARIA - Coding Session Tools

Purpose: Expose coding session management as built-in tools.
"""

from __future__ import annotations

from aria.agents.session import CodingSessionManager
from aria.tools.base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType


class _CodingBaseTool(BaseTool):
    def __init__(self, manager: CodingSessionManager):
        super().__init__()
        self.manager = manager

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN


class StartCodingSessionTool(_CodingBaseTool):
    @property
    def name(self) -> str:
        return "start_coding_session"

    @property
    def description(self) -> str:
        return "Start an external coding agent session in a workspace."

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="workspace", type="string", description="Workspace path", required=True),
            ToolParameter(name="prompt", type="string", description="Task prompt", required=True),
            ToolParameter(name="backend", type="string", description="Backend name", required=False),
            ToolParameter(name="model", type="string", description="Model override", required=False),
            ToolParameter(name="branch", type="string", description="Branch hint", required=False),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        session = await self.manager.start_session(**arguments)
        return ToolResult(tool_name=self.name, status=ToolStatus.SUCCESS, output=session)


class StopCodingSessionTool(_CodingBaseTool):
    @property
    def name(self) -> str:
        return "stop_coding_session"

    @property
    def description(self) -> str:
        return "Stop a running coding session."

    @property
    def parameters(self) -> list[ToolParameter]:
        return [ToolParameter(name="session_id", type="string", description="Session ID", required=True)]

    async def execute(self, arguments: dict) -> ToolResult:
        stopped = await self.manager.stop_session(arguments["session_id"])
        return ToolResult(tool_name=self.name, status=ToolStatus.SUCCESS, output={"stopped": stopped})


class GetCodingOutputTool(_CodingBaseTool):
    @property
    def name(self) -> str:
        return "get_coding_output"

    @property
    def description(self) -> str:
        return "Get recent output from a coding session."

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="session_id", type="string", description="Session ID", required=True),
            ToolParameter(name="lines", type="number", description="Number of lines", required=False, default=50),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        output = self.manager.get_output(arguments["session_id"], lines=int(arguments.get("lines", 50)))
        return ToolResult(tool_name=self.name, status=ToolStatus.SUCCESS, output={"output": output})


class SendToCodingSessionTool(_CodingBaseTool):
    @property
    def name(self) -> str:
        return "send_to_coding_session"

    @property
    def description(self) -> str:
        return "Send input to a running coding session."

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="session_id", type="string", description="Session ID", required=True),
            ToolParameter(name="text", type="string", description="Input text", required=True),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        sent = await self.manager.send_input(arguments["session_id"], arguments["text"])
        return ToolResult(tool_name=self.name, status=ToolStatus.SUCCESS, output={"sent": sent})


class ListCodingSessionsTool(_CodingBaseTool):
    @property
    def name(self) -> str:
        return "list_coding_sessions"

    @property
    def description(self) -> str:
        return "List coding sessions."

    @property
    def parameters(self) -> list[ToolParameter]:
        return [ToolParameter(name="status", type="string", description="Optional status filter", required=False)]

    async def execute(self, arguments: dict) -> ToolResult:
        sessions = await self.manager.list_sessions(status=arguments.get("status"))
        return ToolResult(tool_name=self.name, status=ToolStatus.SUCCESS, output={"sessions": sessions})


class GetCodingDiffTool(_CodingBaseTool):
    @property
    def name(self) -> str:
        return "get_coding_diff"

    @property
    def description(self) -> str:
        return "Get git diff output for a coding session workspace."

    @property
    def parameters(self) -> list[ToolParameter]:
        return [ToolParameter(name="session_id", type="string", description="Session ID", required=True)]

    async def execute(self, arguments: dict) -> ToolResult:
        diff = await self.manager.get_diff(arguments["session_id"])
        return ToolResult(tool_name=self.name, status=ToolStatus.SUCCESS, output={"diff": diff})
