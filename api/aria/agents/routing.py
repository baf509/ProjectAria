"""
ARIA - Coding Task Complexity Routing

Phase: Coding sub-agents
Purpose: Classify a coding task into a complexity tier and pick the model to run
         it on, so planning/design work lands on Opus and scoped work lands on
         Sonnet without anyone having to remember to pin a model.

Three stages, cheap-first:

  1. Heuristic prefilter — unambiguous phrasing is classified for free. Only
     fires on high-confidence patterns; everything else falls through.
  2. Judge — one small Sonnet-class call returning strict JSON. On the `light`
     tier the judge may also answer inline, so trivial lookups never spawn a
     session at all.
  3. Availability — demote to the fallback tier while the Claude subscription
     quota is in cooldown (recorded by the watchdog from pane output).

Sonnet is the floor for normal routing. The sub-Sonnet fallback is reached only
via stage 3.

Related Spec Sections:
- CLAUDE.md: Coding Sub-agents on the Shell Substrate
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings

logger = logging.getLogger(__name__)

# Provider key in the `model_availability` collection. One row per provider;
# `cooled_until` in the future means "don't route here".
CLAUDE_PROVIDER = "claude_code"

# Backends the router would itself have chosen. A caller that names one of
# these is *agreeing* with the router, not overriding it — Hermes passes
# `backend="claude_code"` as belt-and-suspenders, and treating that as a pin
# silently disabled routing for every Hermes-originated task. Naming any other
# backend (codex, pi-code) is a real pin and skips routing entirely. An explicit
# `model` is always a real pin, whatever the backend.
ROUTABLE_BACKENDS = frozenset({CLAUDE_PROVIDER})


def is_routable_backend(backend: Optional[str]) -> bool:
    """True when `backend` leaves the model choice up to the router."""
    return backend is None or backend in ROUTABLE_BACKENDS

TIER_DEEP = "deep"
TIER_STANDARD = "standard"
TIER_LIGHT = "light"
TIER_FALLBACK = "fallback"
VALID_TIERS = (TIER_DEEP, TIER_STANDARD, TIER_LIGHT)

# (compiled pattern, tier, why). First match wins; order matters — the deep
# patterns are checked first so "design a plan to refactor X" reads as deep
# rather than standard. Deliberately narrow: anything not obviously one tier
# should fall through to the judge rather than be guessed at here.
_HEURISTICS: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(
            r"\b(architect(ure)?|design(ing)?|strategi[sz]e|strategy|plan\b|planning"
            r"|trade[- ]?offs?|rfc\b|spec(ification)?\b|evaluate (the )?options"
            r"|how should we|approach for|migration plan|break down)\b",
            re.IGNORECASE,
        ),
        TIER_DEEP,
        "planning/design/strategy language",
    ),
    (
        re.compile(
            r"\b(what is|what'?s|where is|where'?s|which file|look up|find out"
            r"|read (through|the)|summari[sz]e|tell me about|explain how"
            r"|gather|research)\b",
            re.IGNORECASE,
        ),
        TIER_LIGHT,
        "research/information-gathering language",
    ),
    (
        re.compile(
            r"\b(fix|bug|failing test|typo|rename|add a test|write a test"
            r"|implement the|patch|bump|update the (version|dep))\b",
            re.IGNORECASE,
        ),
        TIER_STANDARD,
        "scoped implementation language",
    ),
]

_JUDGE_SYSTEM = """You classify software tasks by the reasoning depth they need, so a \
dispatcher can pick a model. Reply with JSON only — no prose, no code fences.

Schema:
  {"tier": "deep"|"standard"|"light", "why": "<12 words max>", "answer": <string|null>}

Tiers:
  deep     — planning, architecture, design, strategy, evaluating trade-offs,
             cross-cutting refactors, anything where a wrong structural call is
             expensive to undo.
  standard — scoped implementation: write this function, fix this bug, add these
             tests, wire this endpoint. The shape of the work is already clear.
  light    — research and information gathering: look something up, read and
             summarise, answer a factual question. Little or no code written.

"answer": set this ONLY when tier is "light" AND you can answer completely and
correctly from the question text alone, with no need to read files, run
commands, or search. Otherwise set it to null. Never guess about the contents of
a specific repository — if the question is about this user's code, answer null.
"""

_JUDGE_USER = """Classify this task:

<task>
{prompt}
</task>
{workspace_hint}
JSON only."""


@dataclass
class RoutingVerdict:
    """The routing decision for one task."""

    tier: str
    backend: str
    model: Optional[str] = None
    llm: Optional[str] = None
    why: str = ""
    confidence: float = 0.5
    source: str = "heuristic"  # heuristic | judge | fallback | default
    answer: Optional[str] = None  # light tier answered inline; no session needed
    judge_model: Optional[str] = None

    def to_meta(self) -> dict:
        """The subset persisted on a coding_sessions doc, so every surface can
        show *why* a session is on the model it's on."""
        return {
            "tier": self.tier,
            "why": self.why,
            "confidence": self.confidence,
            "source": self.source,
            "judge_model": self.judge_model,
            "decided_at": datetime.now(timezone.utc),
        }

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "backend": self.backend,
            "model": self.model,
            "llm": self.llm,
            "why": self.why,
            "confidence": self.confidence,
            "source": self.source,
            "answer": self.answer,
            "judge_model": self.judge_model,
        }


@dataclass
class _CacheEntry:
    verdict: RoutingVerdict
    expires_at: float


class ComplexityRouter:
    """Classify a coding task and pick the backend/model to run it on."""

    # Process-wide so repeat classifications across requests are free. Keyed by
    # (prompt hash, inline-answer flag) — the judge's reply differs between the
    # two, so they must not share an entry.
    _cache: dict[str, _CacheEntry] = {}

    def __init__(self, db: Optional[AsyncIOMotorDatabase] = None):
        self.db = db

    # ------------------------------------------------------------- public
    async def classify(
        self,
        prompt: str,
        *,
        workspace: Optional[str] = None,
        allow_inline_answer: bool = False,
    ) -> RoutingVerdict:
        """Return the routing verdict for `prompt`.

        `allow_inline_answer` lets the judge answer a `light` task directly
        instead of it becoming a session — used by the interactive desk path.
        Never raises: any failure degrades to the standard tier.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return self._tier_verdict(
                TIER_STANDARD, "empty prompt", 0.0, source="default"
            )

        cache_key = self._cache_key(prompt, allow_inline_answer)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return await self._apply_availability(cached)

        verdict = self._heuristic(prompt)
        if verdict is None:
            verdict = await self._judge(
                prompt, workspace=workspace, allow_inline_answer=allow_inline_answer
            )

        self._cache_put(cache_key, verdict)
        return await self._apply_availability(verdict)

    # ---------------------------------------------------------- stage one
    def _heuristic(self, prompt: str) -> Optional[RoutingVerdict]:
        """Free classification for unambiguous phrasing. None = ask the judge."""
        for pattern, tier, why in _HEURISTICS:
            if pattern.search(prompt):
                return self._tier_verdict(tier, why, 0.7, source="heuristic")
        return None

    # ---------------------------------------------------------- stage two
    async def _judge(
        self,
        prompt: str,
        *,
        workspace: Optional[str],
        allow_inline_answer: bool,
    ) -> RoutingVerdict:
        """One small classification call. Degrades to the standard tier."""
        hint = f"\nWorkspace: {workspace}\n" if workspace else "\n"
        system = _JUDGE_SYSTEM
        if not allow_inline_answer:
            system += '\nThis caller cannot use inline answers: always set "answer" to null.\n'
        user = _JUDGE_USER.format(prompt=prompt[:4000], workspace_hint=hint)

        try:
            if self._resolve_transport() == "cli":
                raw = await self._judge_via_cli(system, user)
            else:
                raw = await self._judge_via_api(system, user)
        except Exception as exc:
            logger.warning("routing judge failed (%s); defaulting to standard", exc)
            return self._tier_verdict(
                TIER_STANDARD, "judge unavailable", 0.0, source="default"
            )

        parsed = self._parse_judge(raw)
        if parsed is None:
            logger.warning("routing judge returned unparseable output; defaulting")
            return self._tier_verdict(
                TIER_STANDARD, "unparseable judge output", 0.0, source="default"
            )

        tier, why, answer = parsed
        verdict = self._tier_verdict(tier, why, 0.9, source="judge")
        verdict.judge_model = settings.coding_routing_judge_model
        if allow_inline_answer and tier == TIER_LIGHT and answer:
            verdict.answer = answer
        return verdict

    @staticmethod
    def _resolve_transport() -> str:
        """Which judge transport to actually use.

        `auto` picks the API when a key is configured and the Claude CLI
        otherwise — so routing works out of the box on a subscription-only box,
        and upgrades to the sub-second path the moment a key is added.
        """
        transport = (settings.coding_routing_judge_transport or "auto").lower()
        if transport in ("api", "cli"):
            return transport
        return "api" if settings.anthropic_api_key else "cli"

    async def _judge_via_api(self, system: str, user: str) -> str:
        """Anthropic API call — sub-second, costs a fraction of a cent."""
        import asyncio

        from aria.llm.base import Message
        from aria.llm.manager import llm_manager

        adapter = llm_manager.get_adapter(
            settings.coding_routing_judge_backend,
            settings.coding_routing_judge_model,
        )
        content, _tool_calls, _usage = await asyncio.wait_for(
            adapter.complete(
                messages=[
                    Message(role="system", content=system),
                    Message(role="user", content=user),
                ],
                temperature=0.0,
                max_tokens=1024,
            ),
            timeout=settings.coding_routing_judge_timeout_seconds,
        )
        return content or ""

    async def _judge_via_cli(self, system: str, user: str) -> str:
        """`claude -p` via ClaudeRunner — burns the subscription, not API tokens."""
        from aria.core.claude_runner import ClaudeRunner

        runner = ClaudeRunner(
            model=settings.coding_routing_judge_model,
            timeout_seconds=settings.coding_routing_judge_timeout_seconds,
            allowed_tools=[],  # classification only — no filesystem, no shell
        )
        output = await runner.run(f"{system}\n\n{user}")
        if output is None:
            raise RuntimeError(runner.last_error or "ClaudeRunner returned nothing")
        return output

    @staticmethod
    def _parse_judge(raw: str) -> Optional[tuple[str, str, Optional[str]]]:
        """Pull (tier, why, answer) out of the judge's reply.

        Tolerates a ```json fence or surrounding prose — models add them despite
        the instruction, and a fenced-but-valid answer is not worth discarding.
        """
        text = (raw or "").strip()
        if not text:
            return None
        fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        if not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                return None
            text = text[start : end + 1]
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        tier = str(data.get("tier") or "").strip().lower()
        if tier not in VALID_TIERS:
            return None
        why = str(data.get("why") or "").strip()[:120]
        answer = data.get("answer")
        if answer is not None and not isinstance(answer, str):
            answer = None
        return tier, why, (answer.strip() if answer else None)

    # -------------------------------------------------------- stage three
    async def _apply_availability(self, verdict: RoutingVerdict) -> RoutingVerdict:
        """Demote to the fallback tier while the Claude quota is in cooldown."""
        if verdict.backend != "claude_code" or self.db is None:
            return verdict
        try:
            cooled_until = await get_cooldown(self.db, CLAUDE_PROVIDER)
        except Exception as exc:  # availability is advisory — never block a spawn
            logger.debug("availability check failed: %s", exc)
            return verdict
        if cooled_until is None:
            return verdict

        mins = max(1, int((cooled_until - datetime.now(timezone.utc)).total_seconds() // 60))
        return RoutingVerdict(
            tier=TIER_FALLBACK,
            backend=settings.coding_routing_fallback_backend,
            model=settings.coding_routing_fallback_model,
            llm=settings.coding_routing_fallback_llm,
            why=f"{verdict.tier} → fallback: Claude quota cooling down ~{mins}m",
            confidence=verdict.confidence,
            source="fallback",
            judge_model=verdict.judge_model,
            answer=verdict.answer,
        )

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _tier_verdict(
        tier: str, why: str, confidence: float, *, source: str
    ) -> RoutingVerdict:
        model = {
            TIER_DEEP: settings.coding_routing_model_deep,
            TIER_STANDARD: settings.coding_routing_model_standard,
            TIER_LIGHT: settings.coding_routing_model_light,
        }.get(tier, settings.coding_routing_model_standard)
        return RoutingVerdict(
            tier=tier,
            backend="claude_code",
            model=model,
            llm=None,
            why=why,
            confidence=confidence,
            source=source,
        )

    @staticmethod
    def _cache_key(prompt: str, allow_inline_answer: bool) -> str:
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        return f"{digest}:{int(allow_inline_answer)}"

    @classmethod
    def _cache_get(cls, key: str) -> Optional[RoutingVerdict]:
        entry = cls._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            cls._cache.pop(key, None)
            return None
        return entry.verdict

    @classmethod
    def _cache_put(cls, key: str, verdict: RoutingVerdict) -> None:
        ttl = max(0, int(settings.coding_routing_cache_ttl_seconds))
        if ttl == 0:
            return
        cls._cache[key] = _CacheEntry(verdict=verdict, expires_at=time.monotonic() + ttl)

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()


# --------------------------------------------------------------------------
# Availability — quota cooldown state, written by the watchdog, read above.
# --------------------------------------------------------------------------

async def record_quota_exhaustion(
    db: AsyncIOMotorDatabase,
    provider: str = CLAUDE_PROVIDER,
    *,
    minutes: Optional[int] = None,
    reason: str = "rate limit / quota detected in session output",
) -> datetime:
    """Mark `provider` as unavailable for a cooldown window.

    ARIA can't see the Claude subscription quota directly — there's no API for
    it. This is the reactive half: the watchdog spots quota text in a session's
    pane output and calls this, and the router demotes until it expires.
    """
    window = minutes if minutes is not None else settings.coding_routing_quota_cooldown_minutes
    now = datetime.now(timezone.utc)
    cooled_until = now + timedelta(minutes=max(1, int(window)))
    await db.model_availability.update_one(
        {"_id": provider},
        {"$set": {"cooled_until": cooled_until, "reason": reason, "detected_at": now}},
        upsert=True,
    )
    logger.warning(
        "provider %s marked unavailable until %s (%s)", provider, cooled_until, reason
    )
    return cooled_until


async def get_cooldown(
    db: AsyncIOMotorDatabase, provider: str = CLAUDE_PROVIDER
) -> Optional[datetime]:
    """Return the active cooldown expiry for `provider`, or None if available."""
    doc = await db.model_availability.find_one({"_id": provider})
    if not doc:
        return None
    cooled_until = doc.get("cooled_until")
    if not isinstance(cooled_until, datetime):
        return None
    # Mongo hands back naive UTC datetimes; make the comparison well-defined.
    if cooled_until.tzinfo is None:
        cooled_until = cooled_until.replace(tzinfo=timezone.utc)
    if cooled_until <= datetime.now(timezone.utc):
        return None
    return cooled_until


async def clear_cooldown(
    db: AsyncIOMotorDatabase, provider: str = CLAUDE_PROVIDER
) -> None:
    """Lift a cooldown early (quota reset sooner than the window assumed)."""
    await db.model_availability.delete_one({"_id": provider})
