"""
ARIA - Coding Session Review

Purpose: Produce review reports for completed coding sessions.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.agents.session import CodingSessionManager


class CodingReviewService:
    """Generate and persist coding session review reports."""

    def __init__(self, db: AsyncIOMotorDatabase, session_manager: CodingSessionManager):
        self.db = db
        self.session_manager = session_manager

    async def review_session(self, session_id: str) -> dict:
        session = await self.session_manager.get_session(session_id)
        if not session:
            raise ValueError("Coding session not found")

        workspace = session["workspace"]
        if not await self._command_exists("git"):
            raise RuntimeError("git is not installed or not in PATH")
        diff_numstat = await self._run_command("git", "-C", workspace, "diff", "--numstat")
        test_result = await self._detect_and_run(workspace, [
            (["pytest", "-q"], "pytest"),
            (["npm", "test", "--", "--runInBand"], "npm test"),
        ])
        lint_result = await self._detect_and_run(workspace, [
            (["ruff", "check", "."], "ruff"),
            (["eslint", "."], "eslint"),
        ])

        if test_result["success"] and lint_result["success"]:
            status = "success"
        elif test_result["ran"] or lint_result["ran"]:
            status = "partial"
        else:
            status = "unknown"

        report = {
            "session_id": session_id,
            "workspace": workspace,
            "status": status,
            "diff_numstat": diff_numstat["stdout"],
            "tests": test_result,
            "lint": lint_result,
            "created_at": datetime.now(timezone.utc),
        }
        await self.db.session_reports.update_one(
            {"session_id": session_id},
            {"$set": report},
            upsert=True,
        )
        return report

    async def get_report(self, session_id: str) -> dict | None:
        return await self.db.session_reports.find_one({"session_id": session_id})

    async def _detect_and_run(self, workspace: str, candidates: list[tuple[list[str], str]]) -> dict:
        for command, label in candidates:
            binary = command[0]
            if not await self._command_exists(binary):
                continue
            result = await self._run_command(*command, cwd=workspace)
            return {
                "ran": True,
                "command": label,
                "success": result["returncode"] == 0,
                "stdout": result["stdout"],
                "stderr": result["stderr"],
            }
        return {"ran": False, "success": False, "stdout": "", "stderr": ""}

    async def _command_exists(self, binary: str) -> bool:
        import shlex
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            f"command -v {shlex.quote(binary)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        return process.returncode == 0

    async def _run_command(self, *command: str, cwd: str | None = None, timeout: float = 120) -> dict:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
            }
        return {
            "returncode": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
