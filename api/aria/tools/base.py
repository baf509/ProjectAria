"""
ARIA - Tool Base Interface

Phase: 3
Purpose: Abstract base class for all tools (built-in and MCP)

Related Spec Sections:
- Section 8.3: Phase 3 - Tools & MCP
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional
from datetime import datetime
from enum import Enum


class ToolType(str, Enum):
    """Type of tool."""
    BUILTIN = "builtin"
    MCP = "mcp"


class ToolStatus(str, Enum):
    """Tool execution status."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


@dataclass
class ToolParameter:
    """Parameter definition for a tool."""
    name: str
    type: str  # "string" | "number" | "boolean" | "object" | "array"
    description: str
    required: bool = False
    default: Any = None
    enum: Optional[list[Any]] = None
    items: Optional[dict] = None  # For array types
    properties: Optional[dict] = None  # For object types


@dataclass
class ToolDefinition:
    """Definition of a tool for LLM consumption."""
    name: str
    description: str
    parameters: list[ToolParameter] = field(default_factory=list)

    def to_json_schema(self) -> dict:
        """Convert to JSON Schema format for LLM."""
        properties = {}
        required = []

        for param in self.parameters:
            param_schema = {
                "type": param.type,
                "description": param.description,
            }

            if param.enum:
                param_schema["enum"] = param.enum
            if param.items:
                param_schema["items"] = param.items
            if param.properties:
                param_schema["properties"] = param.properties
            if param.default is not None:
                param_schema["default"] = param.default

            properties[param.name] = param_schema

            if param.required:
                required.append(param.name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def to_llm_tool(self) -> dict:
        """Convert to LLM tool format (Anthropic/OpenAI compatible)."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.to_json_schema(),
        }


@dataclass
class ToolResult:
    """Result of a tool execution."""
    tool_name: str
    status: ToolStatus
    output: Any = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    metadata: dict = field(default_factory=dict)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def is_success(self) -> bool:
        """Check if execution was successful."""
        return self.status == ToolStatus.SUCCESS

    def is_error(self) -> bool:
        """Check if execution failed."""
        return self.status == ToolStatus.ERROR


class BaseTool(ABC):
    """
    Abstract base class for all tools.
    Both built-in tools and MCP tools inherit from this.
    """

    def __init__(self):
        self._definition: Optional[ToolDefinition] = None

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name of the tool."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does."""
        pass

    @property
    @abstractmethod
    def type(self) -> ToolType:
        """Type of tool (builtin or mcp)."""
        pass

    @property
    @abstractmethod
    def parameters(self) -> list[ToolParameter]:
        """List of parameters this tool accepts."""
        pass

    @property
    def definition(self) -> ToolDefinition:
        """Get the complete tool definition."""
        if self._definition is None:
            self._definition = ToolDefinition(
                name=self.name,
                description=self.description,
                parameters=self.parameters,
            )
        return self._definition

    @abstractmethod
    async def execute(self, arguments: dict) -> ToolResult:
        """
        Execute the tool with given arguments.

        Args:
            arguments: Dictionary of parameter name -> value

        Returns:
            ToolResult with execution outcome
        """
        pass

    async def validate_arguments(self, arguments: dict) -> tuple[bool, Optional[str]]:
        """
        Validate arguments against parameter definitions.

        Returns:
            (is_valid, error_message)
        """
        # Check required parameters
        for param in self.parameters:
            if param.required and param.name not in arguments:
                return False, f"Missing required parameter: {param.name}"

        # Check for unknown parameters
        valid_param_names = {p.name for p in self.parameters}
        for arg_name in arguments.keys():
            if arg_name not in valid_param_names:
                return False, f"Unknown parameter: {arg_name}"

        # Type validation could be added here

        return True, None

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name}, type={self.type})"
