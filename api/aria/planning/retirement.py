"""
ARIA - Project retirement

Phase: Planning / lifecycle
Purpose: End a project deliberately — distil what it taught into long-term
memory, then remove the project record.

WHY THIS EXISTS
---------------
Projects accumulate. The harvester registers anything that looks like one, and
finished work stays on the board forever because deleting it feels like
throwing away the only record of what happened. That is the real objection, and
it is correct: the transcripts ARE the record. So retirement is not a delete —
it is a *transfer*. The durable part moves into `aria.memories` where recall can
reach it; only the board row goes away.

THE ORDER MATTERS
-----------------
Memories are written and VERIFIED before the project is removed. A retirement
that deleted first and then failed to extract would destroy the thing it exists
to preserve, and nothing about "the LLM was down" is visible at the moment of
deletion. If extraction produces nothing, the project is kept and the caller is
told why.

WHAT IS AND IS NOT DELETED
--------------------------
Deleted: the `projects` row, and its tasks (they are scoped to it and mean
nothing without it).
Kept: shells, `shell_events` scrollback, coding sessions, and every memory the
extraction workers already minted. Those are separate lifecycles with their own
retention — retiring a project is not a licence to erase three months of
terminal history, and the reaper/prune workers own that decision.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.memory.extraction import MemoryExtractor
from aria.memory.long_term import LongTermMemory

logger = logging.getLogger(__name__)

# Bounds. `shell_events` holds 17.4M rows across the fleet and a single busy
# shell can carry 7M lines, so "read the transcripts" has to mean "read a
# bounded, recent slice of them" or retirement becomes an unbounded scan that
# also blows the extraction model's context.
MAX_EVENTS_PER_SHELL = 1200
MAX_TOTAL_CHARS = 60_000
MAX_SESSIONS = 40

# States that mean "this is still live" — retiring underneath them would strand
# a running agent whose project vanished mid-session.
LIVE_SESSION_STATES = ("starting", "running", "queued")
LIVE_SHELL_STATES = ("active",)


class RetirementRefused(RuntimeError):
    """Retirement cannot proceed; the message says what to do about it."""


class ProjectRetirementService:
    def __init__(self, db: AsyncIOMotorDatabase, planning, shell_service=None):
        self.db = db
        self.planning = planning
        self.shell_service = shell_service
        self.memory = LongTermMemory(db)
        self.extractor = MemoryExtractor(db)

    # ------------------------------------------------------------- gather ----

    async def _roots(self, project) -> list[str]:
        """Every filesystem root that belongs to this project."""
        roots = []
        for raw in [getattr(project, "path", None), *(getattr(project, "relevant_paths", None) or [])]:
            if raw:
                roots.append(str(raw).rstrip("/"))
        return roots

    def _owns(self, roots: list[str], candidate: Optional[str], all_roots: list[str]) -> bool:
        """Most-specific-root wins — the same rule PathIndex enforces.

        Plain prefix matching would let a coarse parent (the harvested row for
        ~/Development) claim every child project's shells. Here that would mean
        retiring one project and hoovering up another's transcripts.
        """
        if not candidate:
            return False
        c = str(candidate).rstrip("/")
        best = ""
        for r in all_roots:
            if c == r or c.startswith(r + "/"):
                if len(r) > len(best):
                    best = r
        return bool(best) and best in roots

    async def gather(self, project) -> dict[str, Any]:
        """Everything that would be distilled, without touching anything."""
        roots = await self._roots(project)
        all_projects = await self.db.projects.find({}, {"path": 1, "relevant_paths": 1}).to_list(500)
        all_roots: list[str] = []
        for doc in all_projects:
            for raw in [doc.get("path"), *(doc.get("relevant_paths") or [])]:
                if raw:
                    all_roots.append(str(raw).rstrip("/"))

        shells = [
            s for s in await self.db.shells.find({}, {"name": 1, "project_dir": 1, "line_count": 1, "status": 1}).to_list(2000)
            if self._owns(roots, s.get("project_dir"), all_roots)
        ]
        sessions = [
            s for s in await self.db.coding_sessions.find({}).sort("created_at", -1).to_list(500)
            if self._owns(roots, s.get("workspace") or s.get("source_repo"), all_roots)
        ][:MAX_SESSIONS]
        tasks = await self.db.tasks.find({"project_id": project.id}).to_list(500)

        live_sessions = [s for s in sessions if s.get("status") in LIVE_SESSION_STATES]
        live_shells = [s for s in shells if s.get("status") in LIVE_SHELL_STATES]

        return {
            "roots": roots,
            "shells": shells,
            "sessions": sessions,
            "tasks": tasks,
            "live_sessions": live_sessions,
            "live_shells": live_shells,
        }

    async def _transcript_text(self, shells: list[dict]) -> tuple[str, int]:
        """A bounded, most-recent slice of this project's scrollback."""
        chunks: list[str] = []
        used = 0
        for shell in shells:
            name = shell.get("name")
            if not name:
                continue
            events = (
                await self.db.shell_events.find({"shell_name": name}, {"content": 1})
                .sort("line_number", -1)
                .to_list(MAX_EVENTS_PER_SHELL)
            )
            if not events:
                continue
            body = "\n".join(e.get("content", "") for e in reversed(events))
            remaining = MAX_TOTAL_CHARS - used
            if remaining <= 0:
                break
            body = body[-remaining:]
            used += len(body)
            chunks.append(f"=== shell {name} (last {len(events)} lines) ===\n{body}")
        return "\n\n".join(chunks), used

    # -------------------------------------------------------------- retire ----

    def _record_memory(self, project, gathered: dict) -> str:
        """The deterministic record. Written even when the LLM is unavailable.

        Retirement must never be a silent no-op: if extraction fails, this one
        memory is still the durable answer to "what was this project?".
        """
        p = project
        lines = [
            f"Project '{p.name}' (slug {p.slug}) was retired on "
            f"{datetime.now(timezone.utc).date().isoformat()}.",
        ]
        if getattr(p, "summary", None):
            lines.append(f"What it was: {p.summary}")
        if getattr(p, "path", None):
            lines.append(f"Path: {p.path}")
        git = getattr(p, "git", None) or {}
        if isinstance(git, dict) and git.get("branch"):
            lines.append(f"Last branch: {git.get('branch')}")
        if getattr(p, "last_activity_at", None):
            lines.append(f"Last activity: {p.last_activity_at}")
        steps = getattr(p, "next_steps", None) or []
        if steps:
            lines.append("Unfinished at retirement: " + "; ".join(steps[:5]))
        open_tasks = [t.get("title") or t.get("content") for t in gathered["tasks"] if t.get("status") in ("proposed", "active")]
        if open_tasks:
            lines.append("Open tasks at retirement: " + "; ".join(str(t) for t in open_tasks[:5]))
        lines.append(
            f"Scope at retirement: {len(gathered['shells'])} watched shell(s), "
            f"{len(gathered['sessions'])} coding session(s). Scrollback and session "
            f"records were kept; only the project row was removed."
        )
        return "\n".join(lines)

    async def retire(
        self,
        ident: str,
        *,
        dry_run: bool = False,
        delete: bool = True,
    ) -> dict[str, Any]:
        project = await self.planning.get_project_by_slug(ident)
        if project is None:
            project = await self.planning.get_project(ident)
        if project is None:
            raise RetirementRefused(f"No project '{ident}'")

        gathered = await self.gather(project)

        if gathered["live_sessions"] or gathered["live_shells"]:
            raise RetirementRefused(
                f"'{project.slug}' still has "
                f"{len(gathered['live_sessions'])} running session(s) and "
                f"{len(gathered['live_shells'])} active shell(s). Stop them first — "
                "retiring underneath a running agent strands it."
            )

        transcript, chars = await self._transcript_text(gathered["shells"])

        report: dict[str, Any] = {
            "project": project.slug,
            "name": project.name,
            "dry_run": dry_run,
            "shells": [s.get("name") for s in gathered["shells"]],
            "sessions": len(gathered["sessions"]),
            "transcript_chars": chars,
            "memories_written": [],
            "deleted": False,
        }

        record = self._record_memory(project, gathered)
        report["record"] = record

        if dry_run:
            report["would_extract_from_chars"] = chars
            return report

        # 1. The deterministic record always lands first.
        memory_ids = []
        rid = await self.memory.create_memory(
            content=record,
            content_type="event",
            categories=["project", "retirement", project.slug],
            importance=0.8,
            confidence=1.0,
            source={
                "type": "project_retirement",
                "project": project.slug,
                "project_path": getattr(project, "path", None),
                "retired_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if rid:
            memory_ids.append(rid)

        # 2. Then whatever the transcripts themselves yield.
        extracted = []
        if transcript.strip():
            try:
                extracted = await self.extractor.extract_from_text(
                    f"Transcripts from the project '{project.name}' "
                    f"({getattr(project, 'summary', '') or 'no summary'}), being retired. "
                    f"Extract only durable facts, decisions and lessons worth keeping "
                    f"after the project is gone.\n\n{transcript}",
                    llm_backend=settings.shells_extraction_backend,
                    llm_model=settings.shells_extraction_model,
                )
            except Exception as exc:  # extraction is best-effort; the record is not
                logger.warning("retire %s: extraction failed: %s", project.slug, exc)
                report["extraction_error"] = str(exc)[:200]

        for mem in extracted or []:
            content = (mem or {}).get("content")
            if not content:
                continue
            mid = await self.memory.create_memory(
                content=content,
                content_type=mem.get("content_type") or "fact",
                categories=list({*(mem.get("categories") or []), "project", project.slug}),
                importance=float(mem.get("importance") or 0.6),
                confidence=float(mem.get("confidence") or 0.7),
                source={
                    "type": "project_retirement",
                    "project": project.slug,
                    "retired_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            if mid:
                memory_ids.append(mid)

        report["memories_written"] = memory_ids

        # 3. Only now — with the record provably stored — remove the project.
        if not memory_ids:
            raise RetirementRefused(
                f"'{project.slug}' was NOT retired: no memory could be written "
                "(is mongod reachable?). Nothing was deleted."
            )

        stored = await self.db.memories.count_documents(
            {"source.type": "project_retirement", "source.project": project.slug}
        )
        report["memories_verified"] = stored
        if stored == 0:
            raise RetirementRefused(
                f"'{project.slug}' was NOT retired: memories did not persist. Nothing was deleted."
            )

        if delete:
            await self.db.tasks.delete_many({"project_id": project.id})
            ok = await self.planning.delete_project(project.id)
            report["deleted"] = bool(ok)
        return report
