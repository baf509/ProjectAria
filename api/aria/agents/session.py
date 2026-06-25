"""
ARIA - Coding Session Manager

Purpose: Start, stop, and inspect coding-agent subprocess sessions.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.agents.backends.base import StartParams
from aria.agents.backends.registry import BackendRegistry
from aria.agents.backends.tmux import TmuxManager
from aria.agents.checkpoint import (
    build_resume_prompt,
    find_resumable_checkpoint,
    write_checkpoint,
)
from aria.agents.mail import AgentMailbox
from aria.agents.subprocess_mgr import CodingSubprocessManager
from aria.shells.service import ShellService
from aria.config import settings
from aria.notifications.service import NotificationService

import logging

logger = logging.getLogger(__name__)


class CodingSessionManager:
    """Manage coding sessions backed by external CLI agents."""

    def __init__(self, db: AsyncIOMotorDatabase, notification_service: NotificationService | None = None):
        self.db = db
        self.registry = BackendRegistry()
        self.process_manager = CodingSubprocessManager()
        self.tmux_manager = TmuxManager() if TmuxManager.is_available() else None
        # Shell substrate: ARIA-spawned coding sessions run as watched shells so
        # they unify with the fleet (auto-adopt, capture, same drive/observe tools).
        self.shell_service = ShellService(db) if settings.shells_enabled else None
        self.mailbox = AgentMailbox(db)
        self.notification_service = notification_service
        self._watch_tasks: dict[str, asyncio.Task] = {}

    def _use_shell_substrate(self) -> bool:
        return bool(settings.coding_use_shell_substrate and self.shell_service)

    async def start_session(
        self,
        *,
        workspace: str,
        backend: Optional[str],
        prompt: str,
        branch: Optional[str] = None,
        model: Optional[str] = None,
        conversation_id: Optional[str] = None,
        visible: bool = False,
    ) -> dict:
        backend_name = backend or settings.coding_default_backend
        selected_backend = self.registry.get(backend_name)
        workspace_path = os.path.abspath(workspace)
        params = StartParams(workspace=workspace_path, prompt=prompt, model=model, branch=branch)
        command = selected_backend.start_command(params)
        session_id = str(uuid4())
        now = datetime.now(timezone.utc)

        doc = {
            "_id": session_id,
            "backend": backend_name,
            "model": model,
            "workspace": workspace_path,
            "prompt": prompt,
            "branch": branch,
            "conversation_id": conversation_id,
            "visible": visible,
            "status": "starting",
            "pid": None,
            "tmux_pane_id": None,
            "shell_name": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        await self.db.coding_sessions.insert_one(doc)

        # If visible mode requested and tmux is available, spawn in a tmux pane
        if visible and self.tmux_manager:
            shell_cmd = " ".join(command.argv)
            if command.cwd:
                shell_cmd = f"cd {command.cwd} && {shell_cmd}"
            title = f"[{backend_name}] {prompt[:40]}"
            pane = await self.tmux_manager.spawn_pane(session_id, shell_cmd, title=title)
            await self.db.coding_sessions.update_one(
                {"_id": session_id},
                {"$set": {
                    "status": "running",
                    "tmux_pane_id": pane.pane_id,
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
            logger.info("Started visible tmux session %s (pane %s)", session_id, pane.pane_id)
            return await self.get_session(session_id)

        # Shell substrate: run the coding agent as a watched shell so it joins
        # the fleet (auto-adopted + captured to shell_events) and is drivable via
        # the same observe/drive tools. The watchdog/checkpoint/review overlay
        # still manages it through this manager's interface.
        if self._use_shell_substrate():
            argv_str = " ".join(shlex.quote(a) for a in command.argv)
            env_prefix = " ".join(
                f"{k}={shlex.quote(v)}" for k, v in (command.env or {}).items()
            )
            inner = (env_prefix + " " + argv_str).strip()
            launch = "bash -lc " + shlex.quote(inner)
            shell_name = None
            try:
                shell = await self.shell_service.create_shell(
                    name=f"coding-{session_id[:8]}",
                    workdir=command.cwd or workspace_path,
                    launch_command=launch,
                )
                shell_name = shell.name
            except Exception as exc:  # fall back to subprocess substrate
                logger.warning(
                    "shell-substrate spawn failed for %s (%s); using subprocess",
                    session_id, exc,
                )
            if shell_name:
                await self.db.coding_sessions.update_one(
                    {"_id": session_id},
                    {"$set": {
                        "status": "running",
                        "shell_name": shell_name,
                        "updated_at": datetime.now(timezone.utc),
                    }},
                )
                self._watch_tasks[session_id] = asyncio.create_task(
                    self._watch_shell_session(session_id)
                )
                logger.info("Started coding session %s on shell %s", session_id, shell_name)
                return await self.get_session(session_id)

        running = await self.process_manager.spawn(session_id, command)
        await self.db.coding_sessions.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "status": "running",
                    "pid": running.process.pid,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        self._watch_tasks[session_id] = asyncio.create_task(self._watch_session(session_id))
        return await self.get_session(session_id)

    async def stop_session(self, session_id: str) -> bool:
        session = await self.get_session(session_id)
        if not session:
            return False

        # Handle shell-substrate sessions (kill the watched tmux shell)
        if session.get("shell_name") and self.shell_service:
            try:
                await self.shell_service.kill_shell(session["shell_name"])
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("stop: kill_shell failed for %s: %s", session_id, exc)
        # Handle tmux-visible sessions
        elif session.get("tmux_pane_id"):
            if self.tmux_manager:
                await self.tmux_manager.kill_pane(session_id)
            # If tmux_manager is None but session was tmux-based, still mark as stopped
        else:
            stopped = await self.process_manager.stop(session_id)
            if not stopped:
                return False
        await self.db.coding_sessions.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "status": "stopped",
                    "updated_at": datetime.now(timezone.utc),
                    "completed_at": datetime.now(timezone.utc),
                    "exit_code": None,
                }
            },
        )
        watch_task = self._watch_tasks.pop(session_id, None)
        if watch_task is not None:
            watch_task.cancel()
        if self.notification_service:
            try:
                await self.notification_service.notify(
                    source=f"coding:{session_id}",
                    event_type="stopped",
                    detail=f"Stopped coding session in {session['workspace'] if session else 'unknown workspace'}",
                    cooldown_seconds=10,
                )
            except Exception:
                pass
        return True

    async def get_session(self, session_id: str) -> Optional[dict]:
        return await self.db.coding_sessions.find_one({"_id": session_id})

    async def list_sessions(self, status: Optional[str] = None) -> list[dict]:
        query = {"status": status} if status else {}
        return await self.db.coding_sessions.find(query).sort("created_at", -1).to_list(length=200)

    async def get_output(self, session_id: str, lines: int = 50) -> str:
        session = await self.get_session(session_id)
        # Shell-substrate sessions: live ANSI-stripped pane from the fleet.
        if session and session.get("shell_name") and self.shell_service:
            screen = await self.shell_service.current_screen(session["shell_name"], lines=lines)
            return screen or ""
        # Try tmux capture for visible (aria-agents pane) sessions
        if session and session.get("tmux_pane_id") and self.tmux_manager:
            return await self.tmux_manager.capture_output(session_id, lines=lines)
        return self.process_manager.get_output(session_id, lines=lines)

    async def send_input(self, session_id: str, text: str) -> bool:
        session = await self.get_session(session_id)
        if session and session.get("shell_name") and self.shell_service:
            try:
                line, _screen = await self.shell_service.send_input(session["shell_name"], text)
                return line > 0
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("send_input(shell) failed for %s: %s", session_id, exc)
                return False
        return await self.process_manager.send_input(session_id, text)

    async def get_diff(self, session_id: str) -> str:
        session = await self.get_session(session_id)
        if not session:
            return ""
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            session["workspace"],
            "diff",
            "--no-ext-diff",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        return output or error

    async def resume_session(
        self,
        workspace: str,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        conversation_id: Optional[str] = None,
        visible: bool = False,
    ) -> Optional[dict]:
        """Resume a crashed session from its checkpoint.

        Looks for the most recent checkpoint for the given workspace,
        builds a resume prompt with checkpoint context, and starts a
        new session that continues the original work.

        Returns the new session dict, or None if no checkpoint found.
        """
        checkpoint = await find_resumable_checkpoint(self.db, workspace)
        if checkpoint is None:
            return None

        # Load the original session to get the prompt
        original = await self.get_session(checkpoint.session_id)
        if not original:
            # Original session doc deleted — use checkpoint data to resume
            original = {
                "prompt": checkpoint.notes or "Continue previous work",
                "backend": None,
                "model": None,
                "conversation_id": None,
            }

        original_prompt = original.get("prompt", "")
        resume_prompt = build_resume_prompt(checkpoint, original_prompt)

        logger.info(
            "Resuming session from checkpoint %s (branch=%s, commit=%s)",
            checkpoint.session_id, checkpoint.branch, checkpoint.last_commit,
        )

        return await self.start_session(
            workspace=workspace,
            backend=backend or original.get("backend"),
            prompt=resume_prompt,
            branch=checkpoint.branch,
            model=model or original.get("model"),
            conversation_id=conversation_id or original.get("conversation_id"),
            visible=visible,
        )

    async def _watch_session(self, session_id: str) -> None:
        try:
            exit_code = await self.process_manager.wait(session_id)
            if exit_code is None:
                return

            session = await self.get_session(session_id)
            if session and session.get("status") == "stopped":
                return

            # Write checkpoint before marking final status (crash recovery)
            if session and exit_code != 0:
                try:
                    await write_checkpoint(
                        self.db,
                        session_id,
                        session["workspace"],
                        notes=f"Session exited with code {exit_code}",
                    )
                except Exception as e:
                    logger.warning("Failed to write crash checkpoint: %s", e)

            await self.db.coding_sessions.update_one(
                {"_id": session_id},
                {
                    "$set": {
                        "status": "completed" if exit_code == 0 else "failed",
                        "updated_at": datetime.now(timezone.utc),
                        "completed_at": datetime.now(timezone.utc),
                        "exit_code": exit_code,
                    }
                },
            )
            if self.notification_service:
                try:
                    await self.notification_service.notify(
                        source=f"coding:{session_id}",
                        event_type="completed" if exit_code == 0 else "error",
                        detail=f"Session exited with code {exit_code}",
                        cooldown_seconds=10,
                    )
                except Exception:
                    pass

            # Send inter-agent mail notification
            session = await self.get_session(session_id)
            try:
                status = "completed" if exit_code == 0 else "failed"
                output_tail = self.process_manager.get_output(session_id, lines=10)
                await self.mailbox.send_task_done(
                    sender=f"coding:{session.get('backend', 'unknown')}",
                    recipient="orchestrator",
                    session_id=session_id,
                    result_summary=output_tail or f"Session {status}",
                    exit_status=status,
                    conversation_id=session.get("conversation_id") if session else None,
                )
            except Exception as e:
                logger.debug("Failed to send task_done mail: %s", e)

            if session and session.get("conversation_id"):
                summary = f"Coding session {session_id} finished with status {'completed' if exit_code == 0 else 'failed'}."
                try:
                    conv_oid = ObjectId(session["conversation_id"])
                except Exception:
                    return
                await self.db.conversations.update_one(
                    {"_id": conv_oid},
                    {
                        "$push": {
                            "messages": {
                                "id": str(uuid4()),
                                "role": "assistant",
                                "content": summary,
                                "created_at": datetime.now(timezone.utc),
                                "memory_processed": False,
                            }
                        },
                        "$set": {"updated_at": datetime.now(timezone.utc)},
                        "$inc": {"stats.message_count": 1},
                    },
                )
        except asyncio.CancelledError:
            return
        finally:
            self._watch_tasks.pop(session_id, None)

    async def _watch_shell_session(self, session_id: str) -> None:
        """Watch a shell-substrate coding session: poll until its tmux session
        ends (the agent exited), then finalize. The watchdog/checkpoint/review
        overlay manages it in the meantime via this manager's interface, exactly
        as for a subprocess session."""
        try:
            session = await self.get_session(session_id)
            shell_name = (session or {}).get("shell_name")
            if not (shell_name and self.shell_service):
                return
            interval = max(2, settings.coding_watchdog_interval_seconds)
            while True:
                await asyncio.sleep(interval)
                if not await self.shell_service.tmux.has_session(shell_name):
                    break  # tmux session gone -> agent finished
                cur = await self.get_session(session_id)
                if cur and cur.get("status") in ("stopped", "completed", "failed"):
                    return  # finalized elsewhere (e.g. stop_session)

            cur = await self.get_session(session_id)
            if cur and cur.get("status") == "stopped":
                return
            output_tail = await self.get_output(session_id, lines=10)
            now = datetime.now(timezone.utc)
            await self.db.coding_sessions.update_one(
                {"_id": session_id},
                {"$set": {
                    "status": "completed",
                    "updated_at": now,
                    "completed_at": now,
                    "exit_code": None,
                }},
            )
            if self.notification_service:
                try:
                    await self.notification_service.notify(
                        source=f"coding:{session_id}",
                        event_type="completed",
                        detail="Coding session finished",
                        cooldown_seconds=10,
                    )
                except Exception:
                    pass
            try:
                await self.mailbox.send_task_done(
                    sender=f"coding:{(cur or {}).get('backend', 'unknown')}",
                    recipient="orchestrator",
                    session_id=session_id,
                    result_summary=output_tail or "Session completed",
                    exit_status="completed",
                    conversation_id=(cur or {}).get("conversation_id"),
                )
            except Exception as e:
                logger.debug("Failed to send task_done mail: %s", e)
        except asyncio.CancelledError:
            return
        finally:
            self._watch_tasks.pop(session_id, None)
