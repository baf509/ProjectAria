"""Watched Shells — idle-session reaper (capture-then-reap, verified).

ANY watched shell idle past the threshold gets cleaned up — hand-run shells
included, not just ARIA coding sessions (2026-07-30: the original ARIA-only
scoping is gone; the safety property is now "nothing is lost," not "only
touch what ARIA itself spawned"). Before a shell is killed, it's asked to
save what it learned into HANDOFF.md, and — this is the part that matters —
that save is INDEPENDENTLY VERIFIED (the file must exist and have been
modified after the prompt was sent) rather than trusted on the agent's
self-reported done token alone. Same lesson as C1's verification gate: a
self-report is not a confirmation. If the save can't be verified within the
timeout, the shell is left alone and an alert is raised — it is NEVER reaped
on an unconfirmed save.

Related: COHERENCE_DESIGN.md · C9
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from aria.config import settings

logger = logging.getLogger(__name__)

SAVE_PROMPT = (
    "This shell has been idle for over a week and is about to be archived. "
    "Before it is closed, save everything you learned or did here into {handoff} "
    "(create it if it doesn't exist; append if it does) — what you were doing, "
    "what's done, what's left, and anything the next person/agent needs to know. "
    "When you have ACTUALLY WRITTEN the file, reply with {token} alone on its own "
    "line, and stop."
)


def _said_done(screen: Optional[str], token: str) -> bool:
    """True only when the done token appears **alone on a line** — the agent's
    reply. The echoed save prompt mentions the token mid-sentence, so a plain
    substring match would false-positive immediately; anchoring to a standalone
    line avoids that. This is a necessary signal, not a sufficient one -- see
    `_verify_handoff` for the actual gate.
    """
    if not screen:
        return False
    return re.search(rf"(?m)^\s*{re.escape(token)}\s*$", screen) is not None


class ShellReaperWorker:
    """Periodically capture-then-reap any watched shell idle > N days."""

    def __init__(self, shell_service, notification_service=None):
        self.svc = shell_service
        self.db = shell_service.db
        self.notification_service = notification_service
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
        """Any watched shell idle longer than the threshold, protected-tag
        shells excepted. Not scoped to ARIA coding sessions -- a hand-run
        shell gets the same save-then-reap treatment, not exclusion."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.shells_reap_idle_days)
        protected = settings.shells_reap_protected_tag
        out: list[dict] = []
        shells = await self.svc.list_shells(status=["active", "idle"])
        for shell in shells:
            la = shell.last_activity_at
            if la is not None and la.tzinfo is None:
                la = la.replace(tzinfo=timezone.utc)
            if la is None or la > cutoff:
                continue  # not idle long enough
            if protected and protected in (shell.tags or []):
                continue  # protected — never reap
            # If this shell backs a live ARIA coding session, keep that
            # session's record in sync too (status="reaped") -- purely
            # informational bookkeeping, doesn't change reap eligibility.
            coding_session = await self.db.coding_sessions.find_one(
                {"shell_name": shell.name, "status": {"$in": ["running", "starting"]}}
            )
            out.append({"shell": shell, "coding_session": coding_session})
        return out

    async def _mark_reaped(self, coding_session: Optional[dict], *, reason: str) -> None:
        if not coding_session:
            return
        await self.db.coding_sessions.update_one(
            {"_id": coding_session["_id"]},
            {"$set": {
                "status": "reaped",
                "reaped_at": datetime.now(timezone.utc),
                "reap_reason": reason,
            }},
        )

    def _handoff_path(self, shell) -> str:
        project_dir = shell.project_dir or os.path.expanduser("~")
        return os.path.join(project_dir, "HANDOFF.md")

    def _verify_handoff(self, handoff_path: str, prompted_at: datetime) -> bool:
        """Independently verify the save actually happened. A self-reported
        done token is a claim, not a confirmation -- the file must exist AND
        have been modified strictly after the save prompt was sent, so a
        stale/unrelated HANDOFF.md from before the prompt doesn't count."""
        try:
            mtime = os.path.getmtime(handoff_path)
        except OSError:
            return False
        modified_at = datetime.fromtimestamp(mtime, tz=timezone.utc)
        return modified_at > prompted_at

    async def _await_confirmed_save(self, name: str, handoff_path: str, prompted_at: datetime) -> bool:
        """Poll for BOTH signals up to the timeout: the agent's done token
        (alone on a line) AND the independently-verified HANDOFF.md write.
        Either alone is not enough -- a token with no file is an unverified
        claim; a stale file with no token might predate this prompt entirely."""
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
                screen = await self.svc.current_screen(name, lines=60)
            except Exception:
                screen = None
            if _said_done(screen, settings.shells_reap_done_token) and self._verify_handoff(
                handoff_path, prompted_at
            ):
                return True
        return False

    async def _capture_then_reap(self, shell, coding_session: Optional[dict]) -> None:
        name = shell.name
        if not await self.svc.session_alive(name):
            await self._mark_reaped(coding_session, reason="already dead")
            logger.info("reaper: %s already dead — marked reaped", name)
            return

        handoff_path = self._handoff_path(shell)
        prompted_at = datetime.now(timezone.utc)
        confirmed = False
        try:
            await self.svc.send_input(
                name,
                SAVE_PROMPT.format(token=settings.shells_reap_done_token, handoff=handoff_path),
            )
            confirmed = await self._await_confirmed_save(name, handoff_path, prompted_at)
        except Exception as exc:
            logger.warning("reaper: save step failed for %s: %s", name, exc)

        if self._stop.is_set():
            return  # never kill mid-shutdown

        if not confirmed:
            # Per design: no VERIFIED save -> skip and alert, never reap
            # anyway. Left running; picked up again next tick.
            logger.warning(
                "reaper: %s did not produce a verified save within %dm (%s) — "
                "skipping, not reaping",
                name, settings.shells_reap_save_timeout_minutes, handoff_path,
            )
            if self.notification_service:
                try:
                    await self.notification_service.notify(
                        source="shells:reaper",
                        event_type="reap:unconfirmed",
                        detail=(
                            f"Idle shell {name} was prompted to save but "
                            f"{handoff_path} could not be verified written — "
                            "left running, not reaped."
                        ),
                        cooldown_seconds=3600,
                    )
                except Exception:
                    pass
            return

        try:
            await self.svc.kill_shell(name)
        except Exception as exc:
            logger.warning("reaper: kill_shell(%s) failed: %s", name, exc)
            return
        await self._mark_reaped(
            coding_session, reason=f"idle>{settings.shells_reap_idle_days}d, save verified"
        )
        logger.info("reaper: reaped %s (save verified: %s)", name, handoff_path)

    async def _tick(self) -> None:
        if not await self._safety_ok():
            logger.debug("reaper: safety gate closed — skipping tick")
            return
        candidates = await self._candidates()
        if not candidates:
            return
        logger.info("reaper: %d idle shell(s) to reap", len(candidates))
        # NOTE: sequential, one shell at a time -- each can block up to
        # shells_reap_save_timeout_minutes waiting for a confirmed save. A
        # tick with many candidates takes proportionally longer; this is an
        # accepted tradeoff (never reap in parallel while also trying to
        # read/verify each one's save), not an oversight.
        for c in candidates:
            if self._stop.is_set() or not await self._safety_ok():
                break
            await self._capture_then_reap(c["shell"], c["coding_session"])

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
