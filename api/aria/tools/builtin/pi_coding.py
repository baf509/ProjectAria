"""Delegate work to the real upstream Pi coding-agent executable."""

from __future__ import annotations

from aria.agents.session import CodingSessionManager
from aria.tools.base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType


class PiCodingAgentTool(BaseTool):
    """Compatibility tool name backed by an ARIA-managed external Pi shell.

    `pi_coding_agent` used to create an ordinary ARIA conversation and run
    ARIA's orchestrator. That was not Pi. The tool now starts the same real Pi
    CLI coding session exposed by `start_coding_session(backend="pi-code")`.
    """

    def __init__(self, manager: CodingSessionManager):
        super().__init__()
        self._manager = manager

    @property
    def name(self) -> str:
        return "pi_coding_agent"

    @property
    def description(self) -> str:
        return (
            "Start the real Pi coding-agent executable in an ARIA-managed, "
            "watched shell. Pi can read, edit, write, and run commands in the "
            "workspace. The local pi-coding profile is used unless profile is "
            "set to pi-coding-ridge. Returns a coding-session id immediately."
        )

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="task",
                type="string",
                description="Detailed coding task for Pi",
                required=True,
            ),
            ToolParameter(
                name="workspace",
                type="string",
                description="Repository/worktree path in which Pi should run",
                required=True,
            ),
            ToolParameter(
                name="profile",
                type="string",
                description="ARIA Pi launch profile: pi-coding (default) or pi-coding-ridge",
                required=False,
                default="pi-coding",
            ),
            ToolParameter(
                name="create_worktree",
                type="boolean",
                description=(
                    "Run Pi in a dedicated git worktree branched from `workspace` "
                    "(the Guard's default) instead of the live checkout. Omit to "
                    "follow guard_worktree_default; pass false only when the task "
                    "must edit the checkout in place."
                ),
                required=False,
            ),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        task = str(arguments.get("task") or "").strip()
        workspace = str(arguments.get("workspace") or "").strip()
        if not task:
            return ToolResult(
                tool_name=self.name, status=ToolStatus.ERROR,
                error="Task description is required",
            )
        if not workspace:
            return ToolResult(
                tool_name=self.name, status=ToolStatus.ERROR,
                error="Workspace path is required",
            )

        profile = str(arguments.get("profile") or "pi-coding").strip()
        # Absent means "follow guard_worktree_default"; only an explicit false
        # opts Pi back into the live checkout (see StartCodingSessionTool).
        create_worktree = arguments.get("create_worktree")
        try:
            session = await self._manager.start_session(
                workspace=workspace,
                backend=None,
                prompt=task,
                subagent_profile=profile,
                create_worktree=None if create_worktree is None else bool(create_worktree),
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Pi coding session failed to start: {exc}",
            )

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output={
                "session_id": str(session["_id"]),
                "status": session.get("status"),
                "shell_name": session.get("shell_name"),
                "workspace": session.get("workspace"),
                "source_repo": session.get("source_repo"),
                "guard": session.get("guard"),
                "model": session.get("model"),
                "profile": profile,
                "message": "Pi is running as an ARIA coding shell; use coding-session/shell tools to observe or drive it.",
            },
        )
