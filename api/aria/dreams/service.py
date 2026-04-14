"""
ARIA - Dream Cycle Service

Purpose: Periodic offline reflection that runs during quiet hours.
Spawns Claude Code CLI as a subprocess to use subscription tokens
instead of API tokens for the heavy LLM reflection work.

The dream cycle:
1. Collects context (memories, conversations, soul, previous dreams)
2. Builds a reflection prompt
3. Shells out to `claude -p "prompt"` (subscription tokens)
4. Parses structured JSON output
5. Persists journal entries, consolidations, and soul proposals
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.core.prompts import load_prompt
from aria.dreams.collector import DreamCollector
from aria.memory.long_term import LongTermMemory

logger = logging.getLogger(__name__)

# Stale lock threshold — reclaim if holder PID is dead or lock is older than this
_LOCK_STALE_SECONDS = 3600


def _is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class DreamService:
    """Periodic reflection engine that uses Claude Code CLI for inference."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.collector = DreamCollector(db)
        self.long_term_memory = LongTermMemory(db)
        self.runner = ClaudeRunner(timeout_seconds=settings.dream_timeout_seconds)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_run: Optional[datetime] = None
        self._last_status: Optional[str] = None
        self._last_conversation_scan: float = -float("inf")  # monotonic timestamp of last scan

    async def start(self):
        """Start the dream cycle tick loop."""
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info(
            "Dream cycle started (interval=%dh, active_hours=%d-%d)",
            settings.dream_interval_hours,
            settings.dream_active_hours_start,
            settings.dream_active_hours_end,
        )

    async def stop(self):
        """Stop the dream cycle."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Dream cycle stopped")

    async def _tick_loop(self):
        """Sleep for the configured interval, then run a dream cycle."""
        interval = settings.dream_interval_hours * 3600
        while self._running:
            logger.info(
                "Dream tick: sleeping %dh (last_run=%s, last_status=%s)",
                settings.dream_interval_hours,
                self._last_run.isoformat() if self._last_run else "never",
                self._last_status or "none",
            )
            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                await self._run_dream()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Dream cycle error: %s", e, exc_info=True)
                self._last_status = f"error: {e}"

    def _is_active_hours(self) -> bool:
        """Check if current local time is within the dream window."""
        now = datetime.now()
        hour = now.hour
        start = settings.dream_active_hours_start
        end = settings.dream_active_hours_end
        if start <= end:
            return start <= hour < end
        else:
            return hour >= start or hour < end

    async def _acquire_lock(self) -> bool:
        """Acquire a MongoDB-based dream lock. Returns True if acquired."""
        now = datetime.now(timezone.utc)
        pid = os.getpid()

        # Try to reclaim stale locks (dead PID or too old)
        stale_cutoff = now - timedelta(seconds=_LOCK_STALE_SECONDS)
        existing = await self.db.dream_lock.find_one({"_id": "dream_cycle"})

        if existing:
            holder_pid = existing.get("holder_pid")
            acquired_at = existing.get("acquired_at")

            # Lock is live — another process is running a dream
            if (
                holder_pid
                and _is_pid_alive(holder_pid)
                and acquired_at
                and acquired_at > stale_cutoff
            ):
                logger.info(
                    "Dream lock held by PID %d (acquired %s), skipping",
                    holder_pid,
                    acquired_at.isoformat(),
                )
                return False

            # Stale lock — reclaim via atomic update
            result = await self.db.dream_lock.update_one(
                {"_id": "dream_cycle", "holder_pid": holder_pid},
                {"$set": {
                    "holder_pid": pid,
                    "acquired_at": now,
                }},
            )
            return result.modified_count == 1

        # No lock exists — create it atomically
        try:
            await self.db.dream_lock.insert_one({
                "_id": "dream_cycle",
                "holder_pid": pid,
                "acquired_at": now,
            })
            return True
        except Exception:
            # Duplicate key — another process beat us
            return False

    async def _release_lock(self) -> None:
        """Release the dream lock, but only if we still hold it."""
        await self.db.dream_lock.delete_one({
            "_id": "dream_cycle",
            "holder_pid": os.getpid(),
        })

    async def _has_enough_activity(self) -> bool:
        """Check if enough new activity has occurred since the last dream.

        Counts both ARIA conversations and new memories (e.g. from awareness
        digesting Claude Code sessions) so the gate opens regardless of which
        interface the user is active on.

        Throttles the DB scan to once every 10 minutes to avoid repeated
        queries when the time gate passes but this gate doesn't.
        """
        import time

        min_activity = settings.dream_min_conversations
        if min_activity <= 0:
            return True

        # Scan throttle: avoid hitting the DB every tick when time-gate passes
        # but activity-gate doesn't
        now_mono = time.monotonic()
        scan_interval = 600  # 10 minutes
        if now_mono - self._last_conversation_scan < scan_interval:
            return False
        self._last_conversation_scan = now_mono

        # Find last dream run time
        last_dream = await self.db.dream_journal.find_one(
            {}, sort=[("created_at", -1)], projection={"created_at": 1}
        )
        since = last_dream["created_at"] if last_dream else datetime.min.replace(tzinfo=timezone.utc)

        conv_count = await self.db.conversations.count_documents({
            "updated_at": {"$gt": since},
            "status": "active",
        })

        memory_count = await self.db.memories.count_documents({
            "created_at": {"$gt": since},
        })

        total = conv_count + memory_count

        if total < min_activity:
            logger.info(
                "Dream waiting: %d activity since last dream "
                "(%d conversations + %d memories, need %d)",
                total, conv_count, memory_count, min_activity,
            )
            self._last_status = (
                f"waiting: {total}/{min_activity} activity "
                f"({conv_count} convos + {memory_count} memories)"
            )
            return False
        return True

    async def _run_dream(self):
        """Execute a single dream cycle."""
        if not self._is_active_hours():
            now_hour = datetime.now().hour
            logger.info(
                "Dream skipped: outside active hours (now=%d:00, window=%d:00-%d:00)",
                now_hour, settings.dream_active_hours_start, settings.dream_active_hours_end,
            )
            self._last_status = f"skipped: outside active hours (hour={now_hour})"
            return

        if not await self._has_enough_activity():
            return

        if not await self._acquire_lock():
            self._last_status = "skipped: lock held by another process"
            return

        try:
            await self._run_dream_locked()
        finally:
            await self._release_lock()

    async def _run_dream_locked(self):
        """Execute a dream cycle (caller must hold the lock)."""
        logger.info("Dream cycle beginning...")

        # 1. Collect context
        context = await self.collector.collect()

        # 2. Build the reflection prompt
        prompt = load_prompt(
            "dream_reflection",
            soul=context["soul"],
            memories=context["memories"],
            conversations=context["conversations"],
            journal=context["journal"],
        )

        # 3. Run Claude Code CLI
        output = await self._run_claude(prompt)
        if not output:
            self._last_status = "no output from claude"
            return

        # 4. Parse the structured output
        dream_data = self._parse_output(output)
        if not dream_data:
            self._last_status = "failed to parse output"
            return

        # 5. Persist results
        try:
            await self._persist(dream_data)
        except Exception as e:
            self._last_status = f"persist failed: {e}"
            raise

        self._last_run = datetime.now(timezone.utc)
        self._last_status = "completed"
        logger.info("Dream cycle completed successfully")

    async def _run_claude(self, prompt: str) -> Optional[str]:
        """Run prompt through shared ClaudeRunner (subscription tokens)."""
        return await self.runner.run(prompt)

    def _parse_output(self, output: str) -> Optional[dict]:
        """Parse structured JSON from Claude's output."""
        # Try direct JSON parse first
        text = output.strip()

        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        # Try to find JSON object in the output
        # Claude might emit text before/after the JSON
        start = text.find("{")
        if start == -1:
            logger.warning("No JSON object found in dream output")
            logger.debug("Raw output: %s", text[:500])
            return None

        # Find the matching closing brace
        depth = 0
        end = start
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        try:
            data = json.loads(text[start:end])
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse dream JSON: %s", e)
            logger.debug("Attempted to parse: %s", text[start:end][:500])
            return None

        # Validate expected structure
        if not isinstance(data, dict):
            logger.warning("Dream output is not a dict")
            return None

        if "journal_entry" not in data:
            logger.warning("Dream output missing journal_entry")
            return None

        return data

    async def _persist(self, dream_data: dict):
        """Persist dream results to MongoDB."""
        now = datetime.now(timezone.utc)

        # 1. Save the journal entry
        journal_doc = {
            "journal_entry": dream_data.get("journal_entry", ""),
            "connections": dream_data.get("connections", []),
            "knowledge_gaps": dream_data.get("knowledge_gaps", []),
            "soul_proposals": dream_data.get("soul_proposals", []),
            "memory_consolidations_proposed": len(
                dream_data.get("memory_consolidations", [])
            ),
            "stale_memories_flagged": len(
                dream_data.get("stale_memory_ids", [])
            ),
            "created_at": now,
        }
        await self.db.dream_journal.insert_one(journal_doc)
        logger.info("Saved dream journal entry")

        # 2. Process memory consolidations
        consolidations = dream_data.get("memory_consolidations", [])
        for consolidation in consolidations:
            await self._apply_consolidation(consolidation)

        # 3. Prune stale memories flagged by the dream
        stale_ids = dream_data.get("stale_memory_ids", [])
        if stale_ids:
            await self._prune_stale_memories(stale_ids)

        # 4. Store soul proposals (don't auto-apply — require user review)
        soul_proposals = dream_data.get("soul_proposals", [])
        if soul_proposals:
            await self.db.dream_soul_proposals.insert_one({
                "proposals": soul_proposals,
                "status": "pending",
                "created_at": now,
            })
            logger.info(
                "Saved %d soul evolution proposal(s) for review",
                len(soul_proposals),
            )

    async def _prune_stale_memories(self, memory_ids: list[str]):
        """Soft-delete memories flagged as stale by the dream cycle."""
        from bson import ObjectId

        valid_ids = []
        for mid in memory_ids:
            try:
                valid_ids.append(ObjectId(mid))
            except Exception:
                continue

        if not valid_ids:
            return

        result = await self.db.memories.update_many(
            {"_id": {"$in": valid_ids}, "status": "active"},
            {"$set": {
                "status": "pruned",
                "pruned_by": "dream_cycle",
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        logger.info("Pruned %d stale memories", result.modified_count)

    async def _apply_consolidation(self, consolidation: dict):
        """
        Apply a memory consolidation — merge multiple memories into one.
        Soft-deletes originals (status=consolidated) and creates the merged memory.
        """
        memory_ids = consolidation.get("memory_ids", [])
        content = consolidation.get("consolidated_content")
        if not memory_ids or not content:
            return

        try:
            # Create the consolidated memory
            new_id = await self.long_term_memory.create_memory(
                content=content,
                content_type=consolidation.get("content_type", "fact"),
                categories=consolidation.get("categories", []),
                importance=consolidation.get("importance", 0.7),
                source={
                    "type": "dream_consolidation",
                    "original_memory_ids": memory_ids,
                    "consolidated_at": datetime.now(timezone.utc),
                },
            )

            # Soft-delete originals
            from bson import ObjectId
            valid_ids = []
            for mid in memory_ids:
                try:
                    valid_ids.append(ObjectId(mid))
                except Exception:
                    continue

            if valid_ids:
                await self.db.memories.update_many(
                    {"_id": {"$in": valid_ids}},
                    {"$set": {
                        "status": "consolidated",
                        "consolidated_into": new_id,
                        "updated_at": datetime.now(timezone.utc),
                    }},
                )

            logger.info(
                "Consolidated %d memories into %s",
                len(valid_ids), new_id,
            )
        except Exception as e:
            logger.warning("Memory consolidation failed: %s", e)

    async def trigger(self) -> dict:
        """Manually trigger a dream cycle (ignores active hours and conversation gate). Returns result."""
        try:
            if not await self._acquire_lock():
                return {"triggered": False, "error": "Dream lock held by another process"}

            try:
                await self._run_dream_locked()
            finally:
                await self._release_lock()

            # Return the latest journal entry
            latest = await self.db.dream_journal.find_one(
                {}, sort=[("created_at", -1)]
            )
            journal_text = latest.get("journal_entry", "") if latest else ""

            return {
                "triggered": True,
                "status": self._last_status,
                "journal_preview": journal_text[:500],
            }
        except Exception as e:
            return {"triggered": False, "error": str(e)}

    def status(self) -> dict:
        """Return current dream cycle status."""
        return {
            "enabled": settings.dream_enabled,
            "running": self._running,
            "interval_hours": settings.dream_interval_hours,
            "active_hours": {
                "start": settings.dream_active_hours_start,
                "end": settings.dream_active_hours_end,
            },
            "claude_binary": settings.claude_code_binary,
            "claude_model": settings.dream_claude_model or "(default)",
            "timeout_seconds": settings.dream_timeout_seconds,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "last_status": self._last_status,
            "is_active_hours": self._is_active_hours(),
        }
