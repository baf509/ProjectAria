"""
ARIA - Event-Driven Awareness Triggers

Purpose: Rule engine that matches awareness observations to trigger
actions beyond cron-based schedules. Supports pattern matching on
observation category, severity, and content.

Example trigger rules:
    - When CPU > 90%: run diagnostic tool
    - When git has uncommitted changes for > 2h: send reminder
    - When new Claude session activity: update context
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from bson import ObjectId

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


class TriggerRule:
    """A rule that fires when an observation matches criteria."""

    def __init__(
        self,
        name: str,
        category: str,
        severity: Optional[str] = None,
        content_pattern: Optional[str] = None,
        action: str = "notify",
        action_params: Optional[dict] = None,
        cooldown_seconds: int = 300,
        enabled: bool = True,
    ):
        self.name = name
        self.category = category
        self.severity = severity
        self.content_pattern = content_pattern
        self._compiled_pattern = re.compile(content_pattern, re.IGNORECASE) if content_pattern else None
        self.action = action  # "notify" | "prompt" | "tool"
        self.action_params = action_params or {}
        self.cooldown_seconds = cooldown_seconds
        self.enabled = enabled
        self.last_fired_at: Optional[datetime] = None

    def matches(self, observation: dict) -> bool:
        """Check if an observation matches this rule's criteria."""
        if not self.enabled:
            return False

        if observation.get("category") != self.category:
            return False

        if self.severity and observation.get("severity") != self.severity:
            return False

        if self._compiled_pattern:
            content = observation.get("detail", "") or observation.get("summary", "")
            if not self._compiled_pattern.search(content):
                return False

        # Check cooldown
        if self.last_fired_at:
            elapsed = (datetime.now(timezone.utc) - self.last_fired_at).total_seconds()
            if elapsed < self.cooldown_seconds:
                return False

        return True

    def to_doc(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "severity": self.severity,
            "content_pattern": self.content_pattern,
            "action": self.action,
            "action_params": self.action_params,
            "cooldown_seconds": self.cooldown_seconds,
            "enabled": self.enabled,
        }

    @classmethod
    def from_doc(cls, doc: dict) -> "TriggerRule":
        return cls(
            name=doc["name"],
            category=doc["category"],
            severity=doc.get("severity"),
            content_pattern=doc.get("content_pattern"),
            action=doc.get("action", "notify"),
            action_params=doc.get("action_params", {}),
            cooldown_seconds=doc.get("cooldown_seconds", 300),
            enabled=doc.get("enabled", True),
        )


class TriggerEngine:
    """Evaluate observations against trigger rules and fire actions."""

    def __init__(self, db: "AsyncIOMotorDatabase"):
        self.db = db
        self._rules: dict[str, TriggerRule] = {}

    async def load_rules(self) -> int:
        """Load trigger rules from the database."""
        self._rules.clear()
        cursor = self.db.awareness_triggers.find({"enabled": True})
        count = 0
        async for doc in cursor:
            rule = TriggerRule.from_doc(doc)
            self._rules[rule.name] = rule
            count += 1
        logger.info("Loaded %d awareness trigger rule(s)", count)
        return count

    async def add_rule(self, rule: TriggerRule) -> str:
        """Add or update a trigger rule."""
        now = datetime.now(timezone.utc)
        doc = rule.to_doc()
        doc["created_at"] = now
        doc["updated_at"] = now

        result = await self.db.awareness_triggers.update_one(
            {"name": rule.name},
            {"$set": doc},
            upsert=True,
        )
        self._rules[rule.name] = rule
        logger.info("Trigger rule added/updated: %s", rule.name)
        return rule.name

    async def remove_rule(self, name: str) -> bool:
        """Remove a trigger rule."""
        result = await self.db.awareness_triggers.delete_one({"name": name})
        self._rules.pop(name, None)
        return result.deleted_count > 0

    async def evaluate(self, observation: dict) -> list[dict]:
        """Evaluate an observation against all rules.

        Returns list of fired actions with their params.
        """
        fired = []
        for rule in self._rules.values():
            if rule.matches(observation):
                rule.last_fired_at = datetime.now(timezone.utc)
                action_result = await self._fire_action(rule, observation)
                fired.append({
                    "rule": rule.name,
                    "action": rule.action,
                    "result": action_result,
                })

                # Log the trigger event
                await self.db.awareness_trigger_log.insert_one({
                    "rule_name": rule.name,
                    "observation_category": observation.get("category"),
                    "observation_severity": observation.get("severity"),
                    "action": rule.action,
                    "fired_at": datetime.now(timezone.utc),
                })

        return fired

    async def _fire_action(self, rule: TriggerRule, observation: dict) -> str:
        """Execute the action associated with a triggered rule."""
        if rule.action == "notify":
            try:
                from aria.api.deps import _notification_service
                if _notification_service is not None:
                    await _notification_service.notify(
                        source="awareness_trigger",
                        event_type=f"trigger:{rule.name}",
                        detail=rule.action_params.get(
                            "message",
                            f"Trigger '{rule.name}' fired: {observation.get('detail', '')}",
                        ),
                        cooldown_seconds=0,
                    )
                    return "notification_sent"
            except Exception as e:
                logger.error("Trigger notify failed for %s: %s", rule.name, e)
                return f"error: {e}"

        elif rule.action == "tool":
            try:
                from aria.api.deps import get_tool_router
                router = get_tool_router()
                tool_name = rule.action_params.get("tool_name")
                tool_args = rule.action_params.get("tool_args", {})
                if tool_name:
                    result = await router.execute_tool(tool_name, tool_args)
                    return f"tool_executed: {result.status.value}"
            except Exception as e:
                logger.error("Trigger tool execution failed for %s: %s", rule.name, e)
                return f"error: {e}"

        elif rule.action == "prompt":
            try:
                from aria.api.deps import _notification_service
                if _notification_service is not None:
                    message = rule.action_params.get(
                        "message",
                        f"Awareness trigger '{rule.name}': {observation.get('detail', '')}",
                    )
                    await _notification_service.notify(
                        source="awareness_trigger",
                        event_type=f"trigger_prompt:{rule.name}",
                        detail=message,
                        cooldown_seconds=0,
                    )
                    return "prompt_queued"
            except Exception as e:
                logger.error("Trigger prompt failed for %s: %s", rule.name, e)
                return f"error: {e}"

        return "no_action"

    def list_rules(self) -> list[dict]:
        """List all loaded rules."""
        return [rule.to_doc() for rule in self._rules.values()]
