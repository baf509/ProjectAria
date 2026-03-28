"""
ARIA - Llama.cpp Model Switch Tools

Purpose: Expose shared infrastructure model switching to Aria.
"""

from __future__ import annotations

from aria.infrastructure.model_switcher import LlamaCppModelSwitcher
from ..base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType


class ListLlamaCppModelsTool(BaseTool):
    """List available llama.cpp models from the shared infrastructure project."""

    def __init__(self):
        super().__init__()
        self.switcher = LlamaCppModelSwitcher()

    @property
    def name(self) -> str:
        return "list_llamacpp_models"

    @property
    def description(self) -> str:
        return "List available llama.cpp models in the shared infrastructure and mark the active one."

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return []

    async def execute(self, arguments: dict) -> ToolResult:
        models = await self.switcher.list_models()
        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output={"models": [model.to_dict() for model in models]},
        )


class SwitchLlamaCppModelTool(BaseTool):
    """Switch the active llama.cpp model using the infrastructure script."""

    def __init__(self):
        super().__init__()
        self.switcher = LlamaCppModelSwitcher()

    @property
    def name(self) -> str:
        return "switch_llamacpp_model"

    @property
    def description(self) -> str:
        return "Switch the shared llama.cpp model by invoking the infrastructure switch script."

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="model_name",
                type="string",
                description="Model directory or GGUF filename to activate.",
                required=True,
            ),
            ToolParameter(
                name="restart",
                type="boolean",
                description="Whether to recreate the llama.cpp container after switching.",
                required=False,
                default=False,
            ),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        try:
            result = await self.switcher.switch_model(
                model_name=arguments["model_name"],
                restart=bool(arguments.get("restart", False)),
            )
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.SUCCESS,
                output=result,
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=str(exc),
            )
