"""
ARIA - Awareness Service

Purpose: Orchestrates all sensors on configurable intervals, persists
observations to MongoDB with TTLs, and periodically runs a ClaudeRunner-based
analysis to produce situational summaries.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.awareness.base import BaseSensor, Observation
from aria.awareness.triggers import TriggerEngine
from aria.awareness.sensors.claude_sessions import ClaudeSessionSensor
from aria.awareness.sensors.git import GitSensor
from aria.awareness.sensors.system import SystemSensor
from aria.awareness.sensors.filesystem import FilesystemSensor
from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.core.prompts import load_prompt
from aria.memory.long_term import LongTermMemory

logger = logging.getLogger(__name__)


class AwarenessService:
    """Manages passive environmental sensors and their observations."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.long_term_memory = LongTermMemory(db)
        self.trigger_engine = TriggerEngine(db)
        self.sensors: list[BaseSensor] = []
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._analysis_task: Optional[asyncio.Task] = None
        self._digest_task: Optional[asyncio.Task] = None
        self._last_poll: Optional[datetime] = None
        self._last_analysis: Optional[datetime] = None
        self._last_summary: Optional[str] = None

        # Initialize sensors
        self._init_sensors()

    def _init_sensors(self):
        """Create and register sensors based on config."""
        git = GitSensor(watch_dirs=settings.awareness_watch_dirs)
        if git.is_available():
            self.sensors.append(git)

        system = SystemSensor(
            cpu_warn_percent=settings.awareness_cpu_warn_percent,
            memory_warn_percent=settings.awareness_memory_warn_percent,
            disk_warn_percent=settings.awareness_disk_warn_percent,
            check_docker=settings.awareness_check_docker,
        )
        if system.is_available():
            self.sensors.append(system)

        fs = FilesystemSensor(watch_dirs=settings.awareness_watch_dirs)
        self.sensors.append(fs)

        claude = ClaudeSessionSensor(
            max_session_age_hours=settings.awareness_session_lookback_hours,
        )
        if claude.is_available():
            self.sensors.append(claude)

        logger.info(
            "Awareness sensors initialized: %s",
            ", ".join(s.name for s in self.sensors),
        )

    async def start(self):
        """Start the sensor polling and analysis loops."""
        self._running = True

        # Ensure TTL index on observations collection
        await self._ensure_indexes()

        # Load event-driven trigger rules
        await self.trigger_engine.load_rules()

        self._poll_task = asyncio.create_task(self._poll_loop())
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        self._digest_task = asyncio.create_task(self._digest_loop())
        logger.info(
            "Awareness service started (poll=%ds, analysis=%dm)",
            settings.awareness_poll_interval_seconds,
            settings.awareness_analysis_interval_minutes,
        )

    async def stop(self):
        """Stop the sensor polling and analysis loops."""
        self._running = False
        for task in (self._poll_task, self._analysis_task, self._digest_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._poll_task = None
        self._analysis_task = None
        self._digest_task = None
        logger.info("Awareness service stopped")

    async def _ensure_indexes(self):
        """Create indexes for the observations collection."""
        try:
            await self.db.observations.create_index(
                [("created_at", 1)],
                name="observations_ttl",
                expireAfterSeconds=settings.awareness_observation_ttl_hours * 3600,
            )
            await self.db.observations.create_index(
                [("category", 1), ("created_at", -1)],
                name="observations_category_created",
            )
            await self.db.observations.create_index(
                [("severity", 1), ("created_at", -1)],
                name="observations_severity_created",
            )
            await self.db.awareness_summaries.create_index(
                [("created_at", -1)],
                name="awareness_summaries_created",
            )
        except Exception as e:
            logger.warning("Failed to create awareness indexes: %s", e)

    async def _poll_loop(self):
        """Run sensors on a regular interval."""
        interval = settings.awareness_poll_interval_seconds
        while self._running:
            try:
                await self._run_poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Awareness poll error: %s", e)
            await asyncio.sleep(interval)

    async def _run_poll(self):
        """Execute all sensors and persist observations."""
        all_observations: list[Observation] = []

        for sensor in self.sensors:
            try:
                obs = await sensor.poll()
                all_observations.extend(obs)
            except Exception as e:
                logger.warning("Sensor %s failed: %s", sensor.name, e)

        if all_observations:
            docs = [o.to_doc() for o in all_observations]
            await self.db.observations.insert_many(docs)
            logger.debug(
                "Awareness: %d observations from %d sensors",
                len(all_observations), len(self.sensors),
            )

            # Evaluate event-driven triggers against new observations
            for obs in all_observations:
                try:
                    fired = await self.trigger_engine.evaluate(obs.to_doc())
                    if fired:
                        logger.info(
                            "Awareness triggers fired: %s",
                            ", ".join(f["rule"] for f in fired),
                        )
                except Exception as e:
                    logger.warning("Trigger evaluation failed: %s", e)

        self._last_poll = datetime.now(timezone.utc)

    async def _analysis_loop(self):
        """Periodically analyze recent observations via ClaudeRunner."""
        interval = settings.awareness_analysis_interval_minutes * 60
        # Wait one full interval before first analysis
        await asyncio.sleep(interval)
        while self._running:
            try:
                await self._run_analysis()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Awareness analysis error: %s", e)
            await asyncio.sleep(interval)

    async def _run_analysis(self):
        """Analyze recent observations and produce a situational summary."""
        # Gather recent observations
        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=settings.awareness_analysis_interval_minutes * 2
        )
        observations = await self.db.observations.find(
            {"created_at": {"$gte": cutoff}},
        ).sort("created_at", -1).to_list(length=100)

        if not observations:
            logger.debug("Awareness analysis skipped: no recent observations")
            return

        # Format observations for analysis
        obs_lines = []
        for o in observations:
            severity = o.get("severity", "info")
            summary = o.get("summary", "")
            category = o.get("category", "")
            event_type = o.get("event_type", "")
            obs_lines.append(f"[{severity}] {category}/{event_type}: {summary}")

        obs_text = "\n".join(obs_lines)
        prompt = load_prompt("awareness_analysis", observations=obs_text)

        # Use ClaudeRunner for the analysis (subscription tokens)
        summary = None
        if settings.use_claude_runner and ClaudeRunner.is_available():
            runner = ClaudeRunner(timeout_seconds=settings.claude_runner_timeout_seconds)
            summary = await runner.run(prompt)

        if not summary:
            # If ClaudeRunner unavailable, store raw observations as summary
            summary = f"Raw observations ({len(observations)} events):\n{obs_text}"

        self._last_analysis = datetime.now(timezone.utc)
        self._last_summary = summary

        # Persist the summary
        await self.db.awareness_summaries.insert_one({
            "summary": summary,
            "observation_count": len(observations),
            "created_at": self._last_analysis,
        })
        logger.info("Awareness analysis complete: %d observations analyzed", len(observations))

    async def _digest_loop(self):
        """Process Claude Code session digests via ClaudeRunner."""
        # Check for pending digests every 5 minutes
        while self._running:
            await asyncio.sleep(300)
            if not self._running:
                break
            try:
                await self._process_pending_digests()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Session digest error: %s", e)

    async def _process_pending_digests(self):
        """Find session_digest_needed observations and process them."""
        pending = await self.db.observations.find(
            {
                "event_type": "session_digest_needed",
                "digested": {"$ne": True},
            }
        ).sort("created_at", -1).to_list(length=5)

        if not pending:
            return

        if not (settings.use_claude_runner and ClaudeRunner.is_available()):
            logger.debug("Session digest skipped: ClaudeRunner unavailable")
            return

        runner = ClaudeRunner(timeout_seconds=settings.claude_runner_timeout_seconds)

        for obs in pending:
            try:
                detail = obs.get("detail", "")
                if not detail:
                    continue

                prompt = load_prompt("session_digest", session_content=detail)
                result = await runner.run(prompt)

                if not result:
                    logger.warning("ClaudeRunner returned no output for session digest")
                    continue

                # Parse the digest and store as memories
                await self._persist_digest(obs, result)

                # Mark as digested so we don't process again
                await self.db.observations.update_one(
                    {"_id": obs["_id"]},
                    {"$set": {"digested": True}},
                )

            except Exception as e:
                logger.warning("Failed to digest session: %s", e)

    async def _persist_digest(self, obs: dict, digest_text: str):
        """Store a session digest as ARIA long-term memories."""
        import json as _json

        tags = obs.get("tags", [])
        project = tags[0] if tags else "unknown"

        # Try to parse structured JSON from the digest
        memories_to_store = []
        try:
            # Find JSON in the response
            text = digest_text.strip()
            start = text.find("{")
            if start >= 0:
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
                parsed = _json.loads(text[start:end])

                # Extract key takeaways
                takeaways = parsed.get("key_takeaways", [])
                for takeaway in takeaways:
                    if isinstance(takeaway, str) and len(takeaway) > 10:
                        memories_to_store.append({
                            "content": takeaway,
                            "content_type": "fact",
                            "categories": ["claude_session", project],
                            "importance": 0.6,
                        })

                # Extract decisions
                decisions = parsed.get("decisions", [])
                for decision in decisions:
                    if isinstance(decision, str) and len(decision) > 10:
                        memories_to_store.append({
                            "content": decision,
                            "content_type": "preference",
                            "categories": ["claude_session", "decision", project],
                            "importance": 0.7,
                        })

                # Store the summary as a document memory
                summary = parsed.get("summary", "")
                if summary:
                    memories_to_store.append({
                        "content": f"Claude Code session ({project}): {summary}",
                        "content_type": "document",
                        "categories": ["claude_session", project],
                        "importance": 0.5,
                    })

        except (_json.JSONDecodeError, ValueError):
            # If not structured JSON, store the whole digest as one memory
            if len(digest_text.strip()) > 20:
                memories_to_store.append({
                    "content": f"Claude Code session digest ({project}): {digest_text[:2000]}",
                    "content_type": "document",
                    "categories": ["claude_session", project],
                    "importance": 0.5,
                })

        # Persist to long-term memory
        for mem in memories_to_store[:10]:  # Cap at 10 memories per session
            try:
                await self.long_term_memory.create_memory(
                    content=mem["content"],
                    content_type=mem["content_type"],
                    categories=mem["categories"],
                    importance=mem["importance"],
                    source={
                        "type": "claude_session_digest",
                        "project": project,
                        "digested_at": datetime.now(timezone.utc),
                    },
                )
            except Exception as e:
                logger.warning("Failed to store session memory: %s", e)

        if memories_to_store:
            logger.info(
                "Stored %d memories from Claude Code session in %s",
                len(memories_to_store), project,
            )

    async def get_recent_observations(
        self,
        limit: int = 20,
        category: Optional[str] = None,
        severity: Optional[str] = None,
        hours: float = 1.0,
    ) -> list[dict]:
        """Query recent observations."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        query: dict = {"created_at": {"$gte": cutoff}}
        if category:
            query["category"] = category
        if severity:
            query["severity"] = severity
        return await self.db.observations.find(
            query
        ).sort("created_at", -1).to_list(length=limit)

    async def get_context_lines(self, limit: int = 10, hours: float = 1.0) -> list[str]:
        """Get recent observations formatted for LLM context injection."""
        observations = await self.get_recent_observations(limit=limit, hours=hours)
        lines = []
        for o in observations:
            obs = Observation(
                sensor=o["sensor"],
                category=o["category"],
                event_type=o["event_type"],
                summary=o["summary"],
                severity=o.get("severity", "info"),
                created_at=o["created_at"],
            )
            lines.append(obs.to_context_line())
        return lines

    async def get_latest_summary(self) -> Optional[str]:
        """Get the most recent analysis summary."""
        doc = await self.db.awareness_summaries.find_one(
            {}, sort=[("created_at", -1)]
        )
        return doc["summary"] if doc else self._last_summary

    async def trigger_poll(self) -> dict:
        """Manually trigger a sensor poll."""
        before = await self.db.observations.count_documents({})
        await self._run_poll()
        after = await self.db.observations.count_documents({})
        return {
            "triggered": True,
            "new_observations": after - before,
            "sensors": [s.name for s in self.sensors],
        }

    async def trigger_analysis(self) -> dict:
        """Manually trigger an analysis cycle."""
        await self._run_analysis()
        return {
            "triggered": True,
            "summary": self._last_summary,
        }

    def status(self) -> dict:
        """Return current awareness service status."""
        return {
            "enabled": settings.awareness_enabled,
            "running": self._running,
            "sensors": [s.name for s in self.sensors],
            "poll_interval_seconds": settings.awareness_poll_interval_seconds,
            "analysis_interval_minutes": settings.awareness_analysis_interval_minutes,
            "observation_ttl_hours": settings.awareness_observation_ttl_hours,
            "watch_dirs": settings.awareness_watch_dirs,
            "last_poll": self._last_poll.isoformat() if self._last_poll else None,
            "last_analysis": self._last_analysis.isoformat() if self._last_analysis else None,
        }
