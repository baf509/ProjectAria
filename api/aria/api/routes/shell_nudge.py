"""
ARIA - Nudge paused watched shells

Purpose: wake a Claude Code (or any watched) shell that paused at a prompt.
Attempts persist on the shell doc, so a periodic sweep gets three-strikes
semantics for free: each sweep nudges a still-paused shell once; after
`shells_nudge_max_attempts` consecutive failed nudges the sweep enqueues an
alert (source="shells:nudge") that rides the existing alerts → outbox → Signal
path for Ben to confirm/intervene.

Lives in its own module (not routes/shells.py) so the nudge feature is one
self-contained seam: policy knobs in config `shells_nudge_*`, state on the
shell doc (`nudge_attempts`, `nudge_last_at`).

The sweep used to be a Hermes cron (*/15, "ARIA paused-shell nudger") whose only
job was the timer — the state and the three-strikes bookkeeping were always
here. That cron died with its 4B model on 2026-08-10, so the timer moved into
ARIA as `shells/nudge_worker.py`. `nudge_shell_once()` below is the one code
path both the route and that worker call; it exists so deleting the cron loses
nothing and so the two callers can never drift apart.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from aria.api.deps import get_db, get_notification_service, get_shell_service
from aria.config import settings
from aria.notifications.service import NotificationService
from aria.shells.service import ShellService

logger = logging.getLogger(__name__)

router = APIRouter()


class ShellNudgeRequest(BaseModel):
    text: Optional[str] = Field(
        default=None,
        description="Override the nudge text; default is the configured "
        "shells_nudge_default_text (or a bare Enter at a safe "
        "'press enter'-style prompt).",
    )
    wait_ms: Optional[int] = Field(default=None, ge=0, le=30000)
    force: bool = Field(
        default=False,
        description="Nudge even if the shell hasn't been paused for "
        "shells_nudge_min_idle_seconds yet (still requires it to be at a "
        "prompt, and still honors the protected tag).",
    )


def _is_safe_enter_prompt(prompt_line: Optional[str]) -> bool:
    """A 'press enter to continue'-style prompt is answered with a bare Enter
    rather than injected text (mirrors the watchdog's SAFE_PROMPTS split)."""
    if not prompt_line:
        return False
    from aria.agents.watchdog import SAFE_PROMPTS
    return any(p.search(prompt_line) for p in SAFE_PROMPTS)


async def safety_engaged(db) -> bool:
    """True if the killswitch or e-stop is active (same gate the reaper and
    Ralph loop honor — never drive a shell under an engaged stop)."""
    from aria.api.deps import get_killswitch, resolve_estop_manager
    try:
        if get_killswitch().is_active:
            return True
        estop = await resolve_estop_manager(db)
        return await estop.is_active()
    except Exception:  # pragma: no cover - defensive: fail closed
        return True


# Kept as the historical private name; the worker imports the public one.
_safety_engaged = safety_engaged


async def nudge_shell_once(
    name: str,
    *,
    db,
    shell_service: ShellService,
    notifier: NotificationService,
    text: Optional[str] = None,
    wait_ms: Optional[int] = None,
    force: bool = False,
) -> dict:
    """Nudge one paused shell, exactly once. The whole policy lives here.

    Returns a dict for every outcome including the two the HTTP route turns into
    status codes (`not_found` → 404, `safety_engaged` → 409). It returns rather
    than raises so the timer worker — which sweeps a whole fleet — treats a
    missing shell as one skipped row instead of an exception mid-sweep.
    """
    shell = await shell_service.get_shell(name)
    if not shell:
        return {"nudged": False, "reason": "not_found"}

    if await safety_engaged(db):
        return {"nudged": False, "reason": "safety_engaged"}

    if settings.shells_nudge_protected_tag in (shell.tags or []):
        return {"nudged": False, "reason": "protected", "attempts": 0}

    overview = await shell_service.fleet_overview()
    row = next((r for r in overview if r["name"] == name), None)
    if row is None:
        return {"nudged": False, "reason": "not_active"}

    doc = await db.shells.find_one({"name": name}) or {}
    attempts = int(doc.get("nudge_attempts") or 0)

    paused = row["activity_state"] == "blocked" or row["awaiting_input"]
    if not paused:
        if attempts:
            await db.shells.update_one(
                {"name": name}, {"$set": {"nudge_attempts": 0}}
            )
        return {"nudged": False, "reason": "not_paused", "attempts": 0}

    if not force and row["idle_seconds"] < settings.shells_nudge_min_idle_seconds:
        return {
            "nudged": False,
            "reason": "paused_too_recently",
            "idle_seconds": row["idle_seconds"],
            "attempts": attempts,
        }

    now = datetime.now(timezone.utc)
    last_at = doc.get("nudge_last_at")
    if last_at is not None:
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        debounce = timedelta(minutes=settings.shells_nudge_min_interval_minutes)
        if not force and now - last_at < debounce:
            return {"nudged": False, "reason": "recently_nudged", "attempts": attempts}

    attempts += 1
    await db.shells.update_one(
        {"name": name},
        {"$set": {"nudge_attempts": attempts, "nudge_last_at": now}},
    )

    if text is not None:
        nudge_text = text
    elif _is_safe_enter_prompt(row.get("prompt_line")):
        nudge_text = ""  # bare Enter answers 'press enter to continue'
    else:
        nudge_text = settings.shells_nudge_default_text

    effective_wait_ms = wait_ms if wait_ms is not None else settings.shells_nudge_wait_ms
    line, screen = await shell_service.send_input(
        name, nudge_text, wait_ms=effective_wait_ms
    )

    escalated = False
    if attempts >= settings.shells_nudge_max_attempts:
        escalated = True
        await notifier.notify(
            source="shells:nudge",
            event_type="nudge:exhausted",
            detail=(
                f"Shell '{name}' has stayed paused through {attempts} nudges "
                f"(prompt: {row.get('prompt_line') or row.get('last_line') or '?'}). "
                f"It needs a human look."
            ),
            cooldown_seconds=1800,
            project_path=row.get("project_dir"),
        )
        # Start a fresh cycle; the notify cooldown stops immediate re-alerts.
        await db.shells.update_one({"name": name}, {"$set": {"nudge_attempts": 0}})

    return {
        "nudged": True,
        "attempts": attempts,
        "escalated": escalated,
        "line": line,
        "screen": screen,
    }


@router.post("/shells/{name}/nudge")
async def nudge_shell(
    name: str,
    request: ShellNudgeRequest,
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    shell_service: Annotated[ShellService, Depends(get_shell_service)],
    notifier: Annotated[NotificationService, Depends(get_notification_service)],
):
    """HTTP face of `nudge_shell_once` (MCP `nudge_paused_shell` lands here).
    The two "no" answers that are about the caller rather than the shell keep
    their status codes — 404 unknown shell, 409 stop engaged — so the existing
    contract is unchanged."""
    result = await nudge_shell_once(
        name,
        db=db,
        shell_service=shell_service,
        notifier=notifier,
        text=request.text,
        wait_ms=request.wait_ms,
        force=request.force,
    )
    reason = result.get("reason")
    if reason == "not_found":
        raise HTTPException(status_code=404, detail=f"No shell '{name}'")
    if reason == "safety_engaged":
        raise HTTPException(
            status_code=409, detail="killswitch/e-stop engaged; not driving shells"
        )
    return result
