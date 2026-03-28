"""
ARIA - OODA Self-Correction Loop

Purpose: Observe-Orient-Decide-Act loop that evaluates LLM response quality
and retries if below threshold.

Design: First attempt is buffered (not streamed) so it can be evaluated.
Only the final accepted response streams to the client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from aria.config import settings
from aria.core.claude_runner import ClaudeRunner
from aria.core.prompts import load_prompt
from aria.llm.base import LLMAdapter, Message
from aria.llm.manager import llm_manager

logger = logging.getLogger(__name__)


@dataclass
class OODAResult:
    """Result of an OODA evaluation cycle."""
    content: str
    score: float
    attempts: int
    feedback: Optional[str] = None
    accepted: bool = True


class OODALoop:
    """Evaluate and optionally retry LLM responses for quality."""

    def __init__(
        self,
        threshold: float = 0.7,
        max_retries: int = 2,
    ):
        self.threshold = threshold
        self.max_retries = max_retries

    async def evaluate_response(
        self,
        question: str,
        response: str,
        backend: str,
        model: str,
    ) -> tuple[float, str]:
        """Score a response using an LLM evaluator. Returns (score, feedback)."""
        import json

        eval_prompt = load_prompt("ooda_evaluation", question=question, response=response)
        result_text = ""

        # Use ClaudeRunner when available — evaluation is a background cost
        if settings.use_claude_runner and ClaudeRunner.is_available():
            runner = ClaudeRunner(timeout_seconds=settings.claude_runner_timeout_seconds)
            result_text = await runner.run(eval_prompt) or ""

        # Fall back to API adapter
        if not result_text:
            adapter = llm_manager.get_adapter(backend, model)
            messages = [Message(role="user", content=eval_prompt)]
            async for chunk in adapter.stream(
                messages, temperature=0.1, max_tokens=256, stream=False
            ):
                if chunk.type == "text":
                    result_text += chunk.content

        # Parse the JSON score
        try:
            start = result_text.find("{")
            end = result_text.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(result_text[start:end])
                score = float(parsed.get("score", 0.5))
                feedback = parsed.get("feedback", "")
                return min(1.0, max(0.0, score)), feedback
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning("Failed to parse OODA evaluation: %s", result_text[:200])

        return 0.5, "Evaluation parse failed"

    async def process_with_ooda(
        self,
        question: str,
        generate_fn,
        backend: str,
        model: str,
    ) -> OODAResult:
        """
        Generate a response, evaluate it, and retry if below threshold.

        Args:
            question: The user's question
            generate_fn: Async callable that returns the full response text
            backend: LLM backend for evaluation
            model: LLM model for evaluation

        Returns:
            OODAResult with the final accepted response
        """
        best_content = ""
        best_score = 0.0
        best_feedback = ""

        for attempt in range(1, self.max_retries + 2):  # +2 because first attempt + retries
            content = await generate_fn()

            if not content.strip():
                logger.warning("OODA attempt %d produced empty response", attempt)
                continue

            score, feedback = await self.evaluate_response(
                question, content, backend, model
            )
            logger.info(
                "OODA attempt %d: score=%.2f feedback=%s",
                attempt, score, feedback[:100],
            )

            if score > best_score:
                best_content = content
                best_score = score
                best_feedback = feedback

            if score >= self.threshold:
                return OODAResult(
                    content=content,
                    score=score,
                    attempts=attempt,
                    feedback=feedback,
                    accepted=True,
                )

            if attempt > self.max_retries:
                break

        # Return best attempt even if below threshold
        return OODAResult(
            content=best_content,
            score=best_score,
            attempts=self.max_retries + 1,
            feedback=best_feedback,
            accepted=best_score >= self.threshold,
        )
