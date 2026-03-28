"""
ARIA - System Sensor

Purpose: Monitor CPU, memory, disk usage, and Docker container health.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aria.awareness.base import BaseSensor, Observation

logger = logging.getLogger(__name__)


class SystemSensor(BaseSensor):
    """Watches system resources and Docker container health."""

    name = "system"
    category = "system"

    def __init__(
        self,
        cpu_warn_percent: float = 90.0,
        memory_warn_percent: float = 85.0,
        disk_warn_percent: float = 90.0,
        check_docker: bool = True,
    ):
        self.cpu_warn = cpu_warn_percent
        self.memory_warn = memory_warn_percent
        self.disk_warn = disk_warn_percent
        self.check_docker = check_docker

    def is_available(self) -> bool:
        try:
            import psutil  # noqa: F401
            return True
        except ImportError:
            return False

    async def poll(self) -> list[Observation]:
        observations = []

        try:
            import psutil
        except ImportError:
            return observations

        # CPU usage (non-blocking — sample over 1 second in thread)
        loop = asyncio.get_event_loop()
        cpu_pct = await loop.run_in_executor(None, lambda: psutil.cpu_percent(interval=1))
        if cpu_pct >= self.cpu_warn:
            observations.append(Observation(
                sensor=self.name,
                category=self.category,
                event_type="high_cpu",
                summary=f"CPU usage at {cpu_pct:.0f}%",
                severity="warning",
                tags=["cpu"],
            ))

        # Memory
        mem = psutil.virtual_memory()
        if mem.percent >= self.memory_warn:
            used_gb = mem.used / (1024 ** 3)
            total_gb = mem.total / (1024 ** 3)
            observations.append(Observation(
                sensor=self.name,
                category=self.category,
                event_type="high_memory",
                summary=f"Memory at {mem.percent:.0f}% ({used_gb:.1f}/{total_gb:.1f} GB)",
                severity="warning",
                tags=["memory"],
            ))

        # Disk
        disk = psutil.disk_usage("/")
        if disk.percent >= self.disk_warn:
            free_gb = disk.free / (1024 ** 3)
            observations.append(Observation(
                sensor=self.name,
                category=self.category,
                event_type="low_disk",
                summary=f"Disk at {disk.percent:.0f}% — {free_gb:.1f} GB free",
                severity="warning",
                tags=["disk"],
            ))

        # Docker containers
        if self.check_docker:
            docker_obs = await self._check_docker()
            observations.extend(docker_obs)

        return observations

    async def _check_docker(self) -> list[Observation]:
        """Check Docker container health via CLI."""
        observations = []
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.State}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                return observations

            for line in stdout.decode("utf-8", errors="replace").strip().splitlines():
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                name, status, state = parts[0], parts[1], parts[2]
                if state != "running":
                    observations.append(Observation(
                        sensor=self.name,
                        category=self.category,
                        event_type="container_unhealthy",
                        summary=f"Container '{name}' is {state}: {status}",
                        severity="warning",
                        tags=["docker", name],
                    ))

                # Check for containers restarting frequently
                if "Restarting" in status:
                    observations.append(Observation(
                        sensor=self.name,
                        category=self.category,
                        event_type="container_restart_loop",
                        summary=f"Container '{name}' is restarting",
                        severity="warning",
                        tags=["docker", name],
                    ))

        except FileNotFoundError:
            pass  # Docker not installed
        except Exception as e:
            logger.debug("Docker check failed: %s", e)

        return observations
