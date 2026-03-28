"""
ARIA - Autopilot Planner

Purpose: Decompose a high-level goal into executable steps using LLM.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.core.prompts import load_prompt
from aria.llm.base import Message
from aria.llm.manager import llm_manager

logger = logging.getLogger(__name__)


class AutopilotPlanner:
    """Decompose goals into step-by-step plans."""

    async def create_plan(
        self,
        goal: str,
        backend: str = "llamacpp",
        model: str = "default",
        context: str = "",
    ) -> list[dict]:
        """Generate a plan from a goal description."""
        prompt = load_prompt("autopilot_planning",
            goal=goal,
            context=f"Additional context: {context}" if context else "",
        )

        result_text = ""

        # Use ClaudeRunner (subscription tokens) when available
        if settings.use_claude_runner and ClaudeRunner.is_available():
            runner = ClaudeRunner(timeout_seconds=settings.claude_runner_timeout_seconds)
            result_text = await runner.run(prompt) or ""

        # Fall back to API tokens
        if not result_text:
            adapter = llm_manager.get_adapter(backend, model)
            messages = [Message(role="user", content=prompt)]
            async for chunk in adapter.stream(
                messages, temperature=0.3, max_tokens=2048, stream=False
            ):
                if chunk.type == "text":
                    result_text += chunk.content

        # Parse JSON from response
        try:
            start = result_text.find("[")
            end = result_text.rfind("]") + 1
            if start >= 0 and end > start:
                steps = json.loads(result_text[start:end])
                # Validate and normalize
                normalized = []
                for i, step in enumerate(steps):
                    normalized.append({
                        "index": i,
                        "name": step.get("name", f"Step {i+1}"),
                        "action": step.get("action", "llm_query"),
                        "description": step.get("description", ""),
                        "tool_name": step.get("tool_name"),
                        "tool_arguments": step.get("tool_arguments"),
                        "depends_on": step.get("depends_on", []),
                        "status": "pending",
                        "result": None,
                    })
                return normalized
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("Failed to parse plan: %s", exc)

        # Fallback: single step
        return [{
            "index": 0,
            "name": "Execute goal",
            "action": "llm_query",
            "description": goal,
            "tool_name": None,
            "tool_arguments": None,
            "depends_on": [],
            "status": "pending",
            "result": None,
        }]
