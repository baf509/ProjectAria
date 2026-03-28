"""
ARIA - Skill Loader

Purpose: Validate and extract .skill.zip packages.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from aria.config import settings

logger = logging.getLogger(__name__)

REQUIRED_MANIFEST_FIELDS = {"name", "version", "description"}


class SkillValidationError(Exception):
    pass


class SkillLoader:
    """Validate and extract skill packages."""

    def __init__(self):
        self.skills_dir = Path(os.path.expanduser(settings.skills_dir))
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def validate_zip(self, zip_path: str) -> dict:
        """Validate a .skill.zip and return its manifest."""
        if not zipfile.is_zipfile(zip_path):
            raise SkillValidationError("File is not a valid ZIP archive")

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()

            # Check for manifest.json (may be at root or in a subdirectory)
            manifest_path = None
            for name in names:
                if name.endswith("manifest.json") and name.count("/") <= 1:
                    manifest_path = name
                    break

            if manifest_path is None:
                raise SkillValidationError("Missing manifest.json in skill package")

            try:
                manifest = json.loads(zf.read(manifest_path))
            except json.JSONDecodeError as exc:
                raise SkillValidationError(f"Invalid manifest.json: {exc}")

            missing = REQUIRED_MANIFEST_FIELDS - set(manifest.keys())
            if missing:
                raise SkillValidationError(f"Manifest missing required fields: {missing}")

            return manifest

    def extract(self, zip_path: str, manifest: dict) -> Path:
        """Extract a validated skill package to the skills directory."""
        skill_name = manifest["name"]
        target_dir = self.skills_dir / skill_name

        # Remove existing installation
        if target_dir.exists():
            shutil.rmtree(target_dir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            # Extract to temp dir first, then move
            with tempfile.TemporaryDirectory() as tmp:
                zf.extractall(tmp)
                extracted = Path(tmp)

                # Find the root of the skill content
                items = list(extracted.iterdir())
                if len(items) == 1 and items[0].is_dir():
                    # Single directory inside zip
                    source = items[0]
                else:
                    source = extracted

                shutil.copytree(source, target_dir)

        logger.info("Extracted skill '%s' to %s", skill_name, target_dir)
        return target_dir

    def get_skill_dir(self, skill_name: str) -> Optional[Path]:
        """Get the directory for an installed skill."""
        d = self.skills_dir / skill_name
        return d if d.exists() else None

    def remove(self, skill_name: str) -> bool:
        """Remove an installed skill directory."""
        d = self.skills_dir / skill_name
        if d.exists():
            shutil.rmtree(d)
            return True
        return False
