"""
ARIA - Heartbeat Service

Purpose: Periodic agent turns that check HEARTBEAT.md and surface
anything needing the user's attention. Alerts are delivered via
configured notification channels (Signal, Telegram).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.core.prompts import load_prompt
from aria.core.soul import soul_manager
from aria.db.usage import UsageRepo
from aria.llm.base import Message
from aria.llm.manager import llm_manager
from aria.memory.long_term import LongTermMemory
from aria.notifications.service import NotificationService

logger = logging.getLogger(__name__)

_DEFAULT_HEARTBEAT = """\
# ARIA Heartbeat Checklist

<!--
Check these items periodically. Edit this file to customize what ARIA monitors.
Remove or comment out items to disable them.
-->

- Check if any scheduled tasks have failed recently
- Review if there are any pending reminders that were missed
- If it's daytime, consider a lightweight check-in if nothing else is pending
"""



class HeartbeatService:
    """Periodic agent turns that read HEARTBEAT.md and notify if needed."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        notification_service: NotificationService,
    ):
        self.db = db
        self.notification_service = notification_service
        self.long_term_memory = LongTermMemory(db)
        self.usage_repo = UsageRepo(db)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._heartbeat_path = Path(os.path.expanduser(settings.heartbeat_file))
        self._last_run: Optional[datetime] = None
        self._last_result: Optional[str] = None

    async def start(self):
        """Start the heartbeat tick loop."""
        self._running = True
        self._ensure_heartbeat_file()
        self._task = asyncio.create_task(self._tick_loop())
        logger.info(
            "Heartbeat started (interval=%dm, active_hours=%d-%d)",
            settings.heartbeat_interval_minutes,
            settings.heartbeat_active_hours_start,
            settings.heartbeat_active_hours_end,
        )

    async def stop(self):
        """Stop the heartbeat tick loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Heartbeat stopped")

    def _ensure_heartbeat_file(self) -> None:
        """Create HEARTBEAT.md with default template if it doesn't exist."""
        if self._heartbeat_path.exists():
            return
        self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self._heartbeat_path.write_text(_DEFAULT_HEARTBEAT, encoding="utf-8")
        logger.info("Created default HEARTBEAT.md at %s", self._heartbeat_path)

    async def _tick_loop(self):
        """Sleep for the configured interval, then run a heartbeat."""
        interval = settings.heartbeat_interval_minutes * 60
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                await self._run_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Heartbeat tick error: %s", e)

    def _is_active_hours(self) -> bool:
        """Check if current local time is within the active hours window."""
        now = datetime.now()
        hour = now.hour
        start = settings.heartbeat_active_hours_start
        end = settings.heartbeat_active_hours_end
        if start <= end:
            return start <= hour < end
        else:
            # Wraps midnight (e.g., 22-6)
            return hour >= start or hour < end

    def _read_heartbeat_file(self) -> Optional[str]:
        """Read HEARTBEAT.md. Returns None if missing or effectively empty."""
        if not self._heartbeat_path.exists():
            return None
        content = self._heartbeat_path.read_text(encoding="utf-8").strip()
        # Skip if only blank lines, comments, and headers
        meaningful = [
            line for line in content.splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and not line.strip().startswith("<!--")
            and not line.strip().startswith("-->")
        ]
        if not meaningful:
            return None
        return content

    async def _run_heartbeat(self):
        """Execute a single heartbeat check."""
        if not self._is_active_hours():
            logger.debug("Heartbeat skipped: outside active hours")
            return

        checklist = self._read_heartbeat_file()
        if checklist is None:
            logger.debug("Heartbeat skipped: HEARTBEAT.md empty or missing")
            return

        # Build lightweight context
        soul_content = soul_manager.read()

        # Search memories relevant to the heartbeat content
        memory_context = "No relevant memories."
        try:
            memories = await self.long_term_memory.search(
                query=checklist, limit=5
            )
            if memories:
                memory_context = "\n".join(
                    f"- [{m.content_type}] {m.content}" for m in memories
                )
        except Exception as e:
            logger.warning("Heartbeat memory search failed: %s", e)

        # Build the heartbeat prompt
        system_prompt = "You are ARIA, a personal AI agent performing a periodic heartbeat check."
        if soul_content:
            system_prompt += f"\n\n## Agent Identity\n\n{soul_content}"

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M %Z")
        user_message = load_prompt("heartbeat",
            checklist=checklist,
            current_time=current_time,
            memories=memory_context,
        )

        full_prompt = f"{system_prompt}\n\n{user_message}"

        # Use ClaudeRunner (subscription tokens) when available
        response_text = None
        if settings.use_claude_runner and ClaudeRunner.is_available():
            runner = ClaudeRunner(timeout_seconds=settings.claude_runner_timeout_seconds)
            response_text = await runner.run(full_prompt)

        # Fall back to API tokens if ClaudeRunner unavailable or failed
        if not response_text:
            backend = settings.heartbeat_backend or "llamacpp"
            model = settings.heartbeat_model or "default"
            try:
                adapter = llm_manager.get_adapter(backend, model)
                response = await adapter.complete(
                    [
                        Message(role="system", content=system_prompt),
                        Message(role="user", content=user_message),
                    ],
                    temperature=0.3,
                    max_tokens=512,
                )
                response_text = response.content.strip() if response.content else ""
                usage = response.usage or {}
                await self.usage_repo.record(
                    model=model,
                    source="heartbeat",
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    metadata={"backend": backend},
                )
            except Exception as e:
                logger.error("Heartbeat LLM call failed: %s", e)
                return

        self._last_run = datetime.now(timezone.utc)
        self._last_result = response_text

        # Check for HEARTBEAT_OK
        ok_keyword = settings.heartbeat_ok_keyword
        stripped = response_text.strip()
        if stripped == ok_keyword or stripped.startswith(ok_keyword) or stripped.endswith(ok_keyword):
            # Check if there's meaningful content beyond the OK token
            remaining = stripped.replace(ok_keyword, "").strip()
            if len(remaining) <= 300:
                logger.info("Heartbeat OK — nothing needs attention")
                return

        # Something needs attention — deliver via notification
        logger.info("Heartbeat alert: %s", response_text[:200])
        await self.notification_service.notify(
            source="heartbeat",
            event_type="alert",
            detail=response_text,
            cooldown_seconds=0,
        )

    async def trigger(self) -> dict:
        """Manually trigger a heartbeat check. Returns the result."""
        try:
            await self._run_heartbeat()
            return {
                "triggered": True,
                "last_run": self._last_run.isoformat() if self._last_run else None,
                "last_result": self._last_result,
            }
        except Exception as e:
            return {"triggered": False, "error": str(e)}

    def status(self) -> dict:
        """Return current heartbeat status."""
        return {
            "enabled": settings.heartbeat_enabled,
            "running": self._running,
            "interval_minutes": settings.heartbeat_interval_minutes,
            "active_hours": {
                "start": settings.heartbeat_active_hours_start,
                "end": settings.heartbeat_active_hours_end,
            },
            "backend": settings.heartbeat_backend or "llamacpp",
            "model": settings.heartbeat_model or "default",
            "heartbeat_file": str(self._heartbeat_path),
            "heartbeat_file_exists": self._heartbeat_path.exists(),
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "last_result": self._last_result,
            "is_active_hours": self._is_active_hours(),
        }
