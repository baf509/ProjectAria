"""
ARIA - Skills Routes

Purpose: API endpoints for skill package management.
"""

import os
import tempfile
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from aria.api.deps import get_skill_registry
from aria.skills.registry import SkillRegistry
from aria.skills.loader import SkillValidationError

router = APIRouter()


@router.post("/skills/upload")
async def upload_skill(
    file: UploadFile = File(...),
    registry: SkillRegistry = Depends(get_skill_registry),
):
    """Upload and install a .skill.zip package."""
    if not file.filename or not file.filename.endswith(".skill.zip"):
        raise HTTPException(status_code=400, detail="File must be a .skill.zip package")

    # Save to temp file
    with tempfile.NamedTemporaryFile(suffix=".skill.zip", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = await registry.install_skill(tmp_path)
        return result
    except SkillValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        os.unlink(tmp_path)


@router.get("/skills")
async def list_skills(
    registry: SkillRegistry = Depends(get_skill_registry),
):
    """List all installed skills."""
    skills = await registry.list_skills()
    return [
        {
            "name": s["name"],
            "version": s.get("version"),
            "description": s.get("description"),
            "enabled": s.get("enabled", True),
            "has_tool": s.get("has_tool", False),
            "tool_name": s.get("tool_name"),
            "installed_at": s.get("installed_at"),
        }
        for s in skills
    ]


class SkillToggleRequest(BaseModel):
    enabled: bool


@router.patch("/skills/{skill_name}")
async def toggle_skill(
    skill_name: str,
    body: SkillToggleRequest,
    registry: SkillRegistry = Depends(get_skill_registry),
):
    """Enable or disable a skill."""
    updated = await registry.set_enabled(skill_name, body.enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"name": skill_name, "enabled": body.enabled}


@router.delete("/skills/{skill_name}")
async def delete_skill(
    skill_name: str,
    registry: SkillRegistry = Depends(get_skill_registry),
):
    """Uninstall a skill."""
    removed = await registry.uninstall_skill(skill_name)
    if not removed:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"deleted": True, "name": skill_name}
