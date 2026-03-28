"""
ARIA - Skill Registry

Purpose: Install, uninstall, and manage skill packages with dynamic tool registration.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from aria.skills.loader import SkillLoader, SkillValidationError
from aria.tools.base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorDatabase
    from aria.tools.router import ToolRouter

logger = logging.getLogger(__name__)


class SkillPromptTool(BaseTool):
    """A skill-provided tool that returns a prompt template.

    Supports progressive disclosure: only the brief ``description`` is sent to
    the LLM in the tool catalogue.  The full ``instructions`` (if any) are
    prepended to the tool result only when the skill is actually invoked,
    keeping the context window lean.
    """

    def __init__(
        self,
        skill_name: str,
        prompt_text: str,
        tool_description: str = "",
        instructions: str = "",
    ):
        super().__init__()
        self._name = f"skill_{skill_name}"
        self._description = tool_description or f"Skill: {skill_name}"
        self._prompt_text = prompt_text
        self._instructions = instructions

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def instructions(self) -> str:
        """Full usage instructions loaded on-demand (progressive disclosure)."""
        return self._instructions

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="input",
                type="string",
                description="Input to pass to the skill",
                required=False,
                default="",
            ),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        user_input = arguments.get("input", "")
        output = self._prompt_text.replace("{{input}}", user_input)
        # Progressive disclosure: prepend full instructions when invoked
        if self._instructions:
            output = f"[Skill Instructions]\n{self._instructions}\n\n[Skill Output]\n{output}"
        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output=output,
        )


class SkillRegistry:
    """Manage skill installation, loading, and tool registration."""

    def __init__(self, db: "AsyncIOMotorDatabase", tool_router: "ToolRouter"):
        self.db = db
        self.tool_router = tool_router
        self.loader = SkillLoader()
        self._loaded_tools: dict[str, BaseTool] = {}

    async def install_skill(self, zip_path: str) -> dict:
        """Install a skill from a .skill.zip file."""
        manifest = self.loader.validate_zip(zip_path)
        skill_name = manifest["name"]

        # Extract to disk
        skill_dir = self.loader.extract(zip_path, manifest)

        # Try to load tool.py if present
        tool = self._load_tool_module(skill_name, skill_dir)

        # Or use prompt.txt as a simple prompt tool
        if tool is None:
            prompt_path = skill_dir / "prompt.txt"
            if prompt_path.exists():
                prompt_text = prompt_path.read_text()
                # Load optional instructions.md for progressive disclosure
                instructions = ""
                instructions_path = skill_dir / "instructions.md"
                if instructions_path.exists():
                    instructions = instructions_path.read_text()
                tool = SkillPromptTool(
                    skill_name=skill_name,
                    prompt_text=prompt_text,
                    tool_description=manifest.get("description", ""),
                    instructions=instructions or manifest.get("instructions", ""),
                )

        # Register tool
        if tool is not None:
            try:
                self.tool_router.register_tool(tool)
                self._loaded_tools[skill_name] = tool
            except ValueError:
                # Already registered, replace
                self.tool_router._tools[tool.name] = tool
                self._loaded_tools[skill_name] = tool

        # Persist metadata
        now = datetime.now(timezone.utc)
        await self.db.skills.update_one(
            {"name": skill_name},
            {
                "$set": {
                    "name": skill_name,
                    "version": manifest.get("version", "0.0.0"),
                    "description": manifest.get("description", ""),
                    "manifest": manifest,
                    "enabled": True,
                    "has_tool": tool is not None,
                    "tool_name": tool.name if tool else None,
                    "installed_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )

        return {
            "name": skill_name,
            "version": manifest.get("version"),
            "description": manifest.get("description"),
            "has_tool": tool is not None,
            "tool_name": tool.name if tool else None,
        }

    def _load_tool_module(self, skill_name: str, skill_dir: Path) -> Optional[BaseTool]:
        """Dynamically import tool.py from a skill directory."""
        tool_path = skill_dir / "tool.py"
        if not tool_path.exists():
            return None

        try:
            spec = importlib.util.spec_from_file_location(
                f"aria.skills.{skill_name}.tool", str(tool_path)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Look for a class that inherits from BaseTool
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseTool)
                    and attr is not BaseTool
                ):
                    return attr()

            logger.warning("No BaseTool subclass found in %s", tool_path)
            return None
        except Exception as exc:
            logger.error("Failed to load skill tool %s: %s", skill_name, exc, exc_info=True)
            return None

    async def uninstall_skill(self, skill_name: str) -> bool:
        """Uninstall a skill and unregister its tool."""
        tool = self._loaded_tools.pop(skill_name, None)
        if tool is not None:
            self.tool_router._tools.pop(tool.name, None)

        self.loader.remove(skill_name)
        result = await self.db.skills.delete_one({"name": skill_name})
        return result.deleted_count > 0

    async def set_enabled(self, skill_name: str, enabled: bool) -> bool:
        """Enable or disable a skill."""
        result = await self.db.skills.update_one(
            {"name": skill_name},
            {"$set": {"enabled": enabled, "updated_at": datetime.now(timezone.utc)}},
        )
        if not enabled:
            tool = self._loaded_tools.get(skill_name)
            if tool:
                self.tool_router._tools.pop(tool.name, None)
        elif enabled:
            # Re-register if we have a loaded tool
            tool = self._loaded_tools.get(skill_name)
            if tool:
                try:
                    self.tool_router.register_tool(tool)
                except ValueError:
                    self.tool_router._tools[tool.name] = tool

        return result.matched_count > 0

    async def load_installed_skills(self) -> int:
        """Load all installed and enabled skills on startup."""
        count = 0
        async for skill_doc in self.db.skills.find({"enabled": True}):
            skill_name = skill_doc["name"]
            skill_dir = self.loader.get_skill_dir(skill_name)
            if skill_dir is None:
                logger.warning("Skill directory missing for '%s'", skill_name)
                continue

            tool = self._load_tool_module(skill_name, skill_dir)
            if tool is None:
                prompt_path = skill_dir / "prompt.txt"
                if prompt_path.exists():
                    instructions = ""
                    instructions_path = skill_dir / "instructions.md"
                    if instructions_path.exists():
                        instructions = instructions_path.read_text()
                    tool = SkillPromptTool(
                        skill_name=skill_name,
                        prompt_text=prompt_path.read_text(),
                        tool_description=skill_doc.get("description", ""),
                        instructions=instructions or skill_doc.get("manifest", {}).get("instructions", ""),
                    )

            if tool is not None:
                try:
                    self.tool_router.register_tool(tool)
                except ValueError:
                    self.tool_router._tools[tool.name] = tool
                self._loaded_tools[skill_name] = tool
                count += 1

        logger.info("Loaded %d skill(s) on startup", count)
        return count

    async def list_skills(self) -> list[dict]:
        """List all installed skills."""
        return await self.db.skills.find().to_list(length=100)
