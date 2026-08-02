"""
ARIA - Obsidian vault writer (Coherence C6)

Purpose: publish ARIA's long-form outputs (research reports, analyses, design
drafts) as plain markdown into the LiveSync-materialized vault so they land on
every device Ben reads on. Write-only: ARIA never reads Ben's notes here.

Conflict discipline (the load-bearing constraint):
- Namespace partition: writes go ONLY under vault/<Folder>/{Design,Specs,
  Analysis,Research,Planning}/ — never .obsidian/ or .trash/.
- Atomic writes (temp file + rename) so the LiveSync bridge never sees a
  half-written note.
- Never clobber: an existing file modified within the human-edit guard window
  gets a timestamp-suffixed sibling instead of an overwrite.

Related Spec Sections:
- COHERENCE_DESIGN.md C6 (Obsidian long-form surface)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aria.config import settings

logger = logging.getLogger(__name__)

DOC_TYPES = ("Design", "Specs", "Analysis", "Research", "Planning")


def _slugify_title(title: str, max_len: int = 80) -> str:
    clean = re.sub(r"[^\w\s-]", "", title).strip()
    clean = re.sub(r"\s+", " ", clean)
    return (clean or "untitled")[:max_len].strip()


class ObsidianWriter:
    """Atomic, guard-railed markdown publisher into the Obsidian vault."""

    def __init__(self, vault_path: Optional[str] = None):
        self.vault = Path(vault_path or settings.obsidian_vault_path)

    # ------------------------------------------------------------- guards

    def enabled(self) -> bool:
        return bool(settings.obsidian_enabled) and self.vault.is_dir()

    def _folder_for(self, project: Optional[str], doc_type: str) -> Path:
        """vault/<RepoName>/<DocType>/ — `project` may be a repo path (its
        basename is the vault folder, per the project-docs convention) or a
        bare folder name; None falls back to the configured default folder."""
        if doc_type not in DOC_TYPES:
            raise ValueError(f"doc_type must be one of {DOC_TYPES}, got {doc_type!r}")
        name = (
            os.path.basename(project.rstrip("/")) if project else settings.obsidian_default_folder
        )
        if not name or name.startswith("."):
            name = settings.obsidian_default_folder
        return self.vault / name / doc_type

    @staticmethod
    def _recently_modified(path: Path) -> bool:
        try:
            age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        except FileNotFoundError:
            return False
        return age < settings.obsidian_human_edit_guard_minutes * 60

    # ------------------------------------------------------------- writes

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    async def publish(
        self,
        content: str,
        *,
        title: str,
        doc_type: str = "Research",
        project: Optional[str] = None,
    ) -> Optional[str]:
        """Write a new markdown doc into the vault; returns its path, or None
        when publishing is disabled/unavailable. Never raises into the caller's
        main flow — a vault problem must not fail a research run."""
        if not self.enabled():
            return None
        try:
            folder = self._folder_for(project, doc_type)
            now = datetime.now(timezone.utc)
            base = f"{now.strftime('%Y-%m-%d')} {_slugify_title(title)}"
            path = folder / f"{base}.md"
            if path.exists():
                # Never clobber — most likely a same-day re-publish; a sibling
                # with a time suffix keeps both.
                path = folder / f"{base} {now.strftime('%H%M%S')}.md"

            stamp = now.strftime("%Y-%m-%d %H:%M UTC")
            doc = (
                f"# {title}\n\n"
                f"> Published by ARIA on {stamp}.\n\n"
                f"{content.rstrip()}\n"
            )

            def _write() -> None:
                folder.mkdir(parents=True, exist_ok=True)
                self._atomic_write(path, doc)

            await asyncio.to_thread(_write)
            logger.info("obsidian: published %s", path)
            return str(path)
        except Exception as exc:
            logger.warning("obsidian publish failed for '%s': %s", title, exc)
            return None

    async def append_section(
        self,
        rel_or_abs_path: str,
        heading: str,
        content: str,
        *,
        project: Optional[str] = None,
        doc_type: str = "Analysis",
    ) -> Optional[str]:
        """Append a timestamped `## heading` section to an existing co-drafted
        doc (creating it if absent). Skips — returning None — if a human
        touched the file within the guard window; co-drafting never races
        Ben's own edits."""
        if not self.enabled():
            return None
        try:
            path = Path(rel_or_abs_path)
            if not path.is_absolute():
                path = self._folder_for(project, doc_type) / rel_or_abs_path
            if self._recently_modified(path):
                logger.info("obsidian: skipping %s (recently human-modified)", path)
                return None
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            section = f"\n\n## {heading}\n\n*({stamp})*\n\n{content.rstrip()}\n"

            def _write() -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                existing = path.read_text(encoding="utf-8") if path.exists() else ""
                self._atomic_write(path, existing.rstrip() + section)

            await asyncio.to_thread(_write)
            return str(path)
        except Exception as exc:
            logger.warning("obsidian append failed for '%s': %s", rel_or_abs_path, exc)
            return None
