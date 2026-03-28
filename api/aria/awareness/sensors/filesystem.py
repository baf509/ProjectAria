"""
ARIA - Filesystem Sensor

Purpose: Monitor watched directories for new/modified files, config changes,
and growing log files.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from aria.awareness.base import BaseSensor, Observation
from aria.config import settings

logger = logging.getLogger(__name__)


class FilesystemSensor(BaseSensor):
    """Watches directories for filesystem changes."""

    name = "filesystem"
    category = "filesystem"

    def __init__(
        self,
        watch_dirs: Optional[list[str]] = None,
        config_patterns: Optional[list[str]] = None,
        log_patterns: Optional[list[str]] = None,
        max_file_age_minutes: int = 30,
    ):
        self.watch_dirs = watch_dirs or [settings.coding_default_workspace]
        self.config_patterns = config_patterns or [
            ".env", "docker-compose.yml", "docker-compose.yaml",
            "Dockerfile", "requirements.txt", "package.json",
            "tsconfig.json", "pyproject.toml",
        ]
        self.log_patterns = log_patterns or [".log", ".err"]
        self.max_age = max_file_age_minutes * 60
        self._last_sizes: dict[str, int] = {}  # path -> last known size
        self._known_files: dict[str, set[str]] = {}  # dir -> set of filenames

    def is_available(self) -> bool:
        return True

    async def poll(self) -> list[Observation]:
        observations = []
        now = datetime.now(timezone.utc).timestamp()

        for watch_dir in self.watch_dirs:
            watch_dir = os.path.expanduser(watch_dir)
            if not os.path.isdir(watch_dir):
                continue

            try:
                obs = self._scan_dir(watch_dir, now)
                observations.extend(obs)
            except Exception as e:
                logger.warning("FilesystemSensor error for %s: %s", watch_dir, e)

        return observations

    def _scan_dir(self, dir_path: str, now: float) -> list[Observation]:
        observations = []
        dir_name = os.path.basename(dir_path)

        try:
            entries = os.listdir(dir_path)
        except PermissionError:
            return observations

        current_files = set(entries)
        prev_files = self._known_files.get(dir_path, set())

        # Detect new top-level files (skip on first poll to avoid noise)
        if prev_files:
            new_files = current_files - prev_files
            if new_files:
                # Filter out hidden/temp files
                notable = [f for f in new_files if not f.startswith(".") and not f.endswith("~")]
                if notable and len(notable) <= 10:
                    observations.append(Observation(
                        sensor=self.name,
                        category=self.category,
                        event_type="new_files",
                        summary=f"{dir_name}: {len(notable)} new file(s): {', '.join(sorted(notable)[:5])}",
                        severity="info",
                        tags=[dir_name],
                    ))

        self._known_files[dir_path] = current_files

        # Check config files for recent modifications
        for entry in entries:
            if entry not in self.config_patterns:
                continue
            full_path = os.path.join(dir_path, entry)
            if not os.path.isfile(full_path):
                continue
            try:
                mtime = os.path.getmtime(full_path)
                age = now - mtime
                if age < self.max_age:
                    observations.append(Observation(
                        sensor=self.name,
                        category=self.category,
                        event_type="config_changed",
                        summary=f"{dir_name}/{entry} modified {int(age // 60)}m ago",
                        severity="notice",
                        tags=[dir_name, "config"],
                    ))
            except OSError:
                continue

        # Check for growing log files
        for entry in entries:
            if not any(entry.endswith(pat) for pat in self.log_patterns):
                continue
            full_path = os.path.join(dir_path, entry)
            if not os.path.isfile(full_path):
                continue
            try:
                size = os.path.getsize(full_path)
                prev_size = self._last_sizes.get(full_path, size)
                growth = size - prev_size
                self._last_sizes[full_path] = size

                # Alert if log grew more than 1MB since last poll
                if growth > 1_048_576:
                    growth_mb = growth / (1024 * 1024)
                    observations.append(Observation(
                        sensor=self.name,
                        category=self.category,
                        event_type="log_growth",
                        summary=f"{dir_name}/{entry} grew {growth_mb:.1f} MB since last check",
                        severity="notice",
                        tags=[dir_name, "logs"],
                    ))

                # Alert if log is very large (>500MB)
                if size > 524_288_000:
                    size_mb = size / (1024 * 1024)
                    observations.append(Observation(
                        sensor=self.name,
                        category=self.category,
                        event_type="large_log",
                        summary=f"{dir_name}/{entry} is {size_mb:.0f} MB",
                        severity="warning",
                        tags=[dir_name, "logs"],
                    ))
            except OSError:
                continue

        return observations
