"""Watched Shells — idle-session reaper (capture-then-reap).

ARIA-spawned coding sessions (`claude-coding-*` watched shells) that go idle for
more than a week are cleaned up — but not before the agent is asked to save
whatever it learned into documentation (per the `project-docs` convention). Even
if that save times out, nothing is lost: scrollback is already mined into memory
by the extraction worker.

Related: COHERENCE_DESIGN.md · C9
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from aria.config import settings

logger = logging.getLogger(__name__)

SAVE_PROMPT = (
    "This coding session has been idle for over a week and is about to be archived. "
    "Before it is closed, save everything you learned or did here into the appropriate "
    "documentation — follow the project-docs convention (design/analysis/planning notes go "
    "to the project's Obsidian vault folder; a working pick-up point goes to HANDOFF). "
    "When you have finished saving, reply with {token} alone on its own line, and stop."
)


def _save_done(screen: Optional[str], token: str) -> bool:
    """True only when the done token appears **alone on a line** — the agent's
    reply. The echoed save prompt mentions the token mid-sentence, so a plain
    substring match would false-positive immediately; anchoring to a standalone
    line avoids that.
    """
    if not screen:
        return False
    return re.search(rf"(?m)^\s*{re.escape(token)}\s*$", screen) is not None


class ShellReaperWorker:
    """Periodically capture-then-reap ARIA coding sessions idle > N days."""

    def __init__(self, shell_service):
        self.svc = shell_service
        self.db = shell_service.db
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="shells.reaper")
        logger.info(
            "shells reaper started (idle>%dd, every %dh)",
            settings.shells_reap_idle_days, settings.shells_reap_interval_hours,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def _safety_ok(self) -> bool:
        """False (skip reaping) if the killswitch or e-stop is engaged."""
        from aria.api.deps import get_killswitch, resolve_estop_manager
        try:
            if get_killswitch().is_active:  # property, not a method
                return False
            estop = await resolve_estop_manager(self.db)
            if await estop.is_active():
                return False
        except Exception as exc:  # fail closed
            logger.warning("reaper: safety check failed, skipping tick: %s", exc)
            return False
        return True

    async def _candidates(self) -> list[dict]:
        """ARIA coding-session shells idle longer than the threshold."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.shells_reap_idle_days)
        protected = settings.shells_reap_protected_tag
        out: list[dict] = []
        cursor = self.db.coding_sessions.find(
            {"shell_name": {"$ne": None}, "status": {"$in": ["running", "starting"]}}
        )
        async for cs in cursor:
            name = cs.get("shell_name")
            if not name:
                continue
            shell = await self.svc.get_shell(name)
            if shell is None:
                # session points at a shell that no longer exists → just clean up
                await self._mark_reaped(cs, reason="shell gone")
                continue
            la = shell.last_activity_at
            if la is not None and la.tzinfo is None:
                la = la.replace(tzinfo=timezone.utc)
            if la is None or la > cutoff:
                continue  # not idle long enough
            if protected and protected in (shell.tags or []):
                continue  # protected — never reap
            out.append({"cs": cs, "name": name})
        return out

    async def _mark_reaped(self, cs: dict, *, reason: str) -> None:
        await self.db.coding_sessions.update_one(
            {"_id": cs["_id"]},
            {"$set": {
                "status": "reaped",
                "reaped_at": datetime.now(timezone.utc),
                "reap_reason": reason,
            }},
        )

    async def _await_save(self, name: str) -> bool:
        """Poll the pane for the done token (alone on a line), up to the timeout."""
        token = settings.shells_reap_done_token
        deadline = max(0, settings.shells_reap_save_timeout_minutes) * 60
        poll = 15
        waited = 0
        while waited < deadline:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll)
                return False  # shutting down
            except asyncio.TimeoutError:
                pass
            waited += poll
            try:
                if _save_done(await self.svc.current_screen(name, lines=60), token):
                    return True
            except Exception:
                pass
        return False

    async def _capture_then_reap(self, cs: dict, name: str) -> None:
        if not await self.svc.session_alive(name):
            await self._mark_reaped(cs, reason="already dead")
            logger.info("reaper: %s already dead — marked reaped", name)
            return

        saved = False
        try:
            await self.svc.send_input(name, SAVE_PROMPT.format(token=settings.shells_reap_done_token))
            saved = await self._await_save(name)
        except Exception as exc:
            logger.warning("reaper: save step failed for %s: %s", name, exc)

        if self._stop.is_set():
            return  # never kill mid-shutdown

        try:
            await self.svc.kill_shell(name)
        except Exception as exc:
            logger.warning("reaper: kill_shell(%s) failed: %s", name, exc)
        await self._mark_reaped(
            cs, reason=f"idle>{settings.shells_reap_idle_days}d (saved={saved})"
        )
        logger.info("reaper: reaped %s (saved=%s)", name, saved)

    async def _tick(self) -> None:
        if not await self._safety_ok():
            logger.debug("reaper: safety gate closed — skipping tick")
            return
        candidates = await self._candidates()
        if not candidates:
            return
        logger.info("reaper: %d idle coding session(s) to reap", len(candidates))
        for c in candidates:
            if self._stop.is_set() or not await self._safety_ok():
                break
            await self._capture_then_reap(c["cs"], c["name"])

    async def _run(self) -> None:
        interval = max(1, int(settings.shells_reap_interval_hours)) * 3600
        # Defer the first run so startup stays light.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=120)
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # pragma: no cover
                logger.warning("reaper tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
