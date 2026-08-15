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

Re-routing (added for the escalation ladder, proposal §6.2 L3) is a SEPARATE
entry point: `reroute()`. Spawn-time routing above is one-shot and only ever
demotes (stage 3); the ladder needs the opposite move — a *failed* session
promoted one rung up a fixed strength ladder, honouring the project charter's
`tiers_allowed`. Nothing in `classify()` or its stages changes: a task that has
not failed still routes exactly as before.

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


def judge_transport() -> str:
    """Which Claude transport a small judge/review call should use.

    `auto` picks the API when a key is configured and the Claude CLI otherwise —
    so this works out of the box on a subscription-only box, and upgrades to the
    sub-second path the moment a key is added. Public because the different-
    family reviewer (agents/review.py) needs exactly this decision and must not
    fork a second, drifting copy of it.
    """
    transport = (settings.coding_routing_judge_transport or "auto").lower()
    if transport in ("api", "cli"):
        return transport
    return "api" if settings.anthropic_api_key else "cli"

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


class QuotaCooldownError(RuntimeError):
    """Claude quota is in cooldown and no fallback backend is configured.

    Deliberately distinct from every other routing failure: routing is
    otherwise advisory and degrades to the standard tier, but this one must
    reach the caller and stop the spawn.
    """


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
        """Which judge transport to actually use (see `judge_transport`)."""
        return judge_transport()

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

        # No fallback configured = fail and pause, on purpose. Raising a
        # DEDICATED type matters: start_session wraps routing in a broad
        # `except Exception -> use defaults`, so a generic raise here would be
        # swallowed and the task would run on claude_code anyway — straight
        # into the exhausted quota. QuotaCooldownError is re-raised by that
        # caller ahead of the generic handler.
        if not settings.coding_routing_fallback_backend:
            raise QuotaCooldownError(
                f"Claude quota is cooling down for ~{mins}m and no fallback "
                f"backend is configured (coding_routing_fallback_backend is "
                f"empty, by design). Not silently downgrading the model — "
                f"retry after the cooldown, or pin a backend/model explicitly."
            )

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


# --------------------------------------------------------------------------
# Re-route — the ladder's L3 rung (proposal §6.2).
#
# Spawn-time routing is one-shot and only demotes. When a session has already
# failed, the supervisor needs the opposite: move it ONE rung up a fixed
# strength ladder, carrying the failure history into the prompt, and only
# within the tiers the project's charter allows. None of the code above is
# involved — a re-route is not a re-classification, because the task's
# complexity is not what changed; the evidence that the current tier cannot do
# it is.
# --------------------------------------------------------------------------

# Charter vocabulary (planning/models.py Charter.tiers_allowed).
TIER_LOCAL = "local"
TIER_RIDGE = "ridge"
TIER_RED = "red"
TIER_CLOUD = "cloud"
CHARTER_TIERS = (TIER_LOCAL, TIER_RIDGE, TIER_RED, TIER_CLOUD)


@dataclass(frozen=True)
class Rung:
    """One step on the escalation ladder.

    `profile` names a `db.agents` launch row rather than a backend/model pair
    because that is how the remote coders are actually configured (pi-coding-
    ridge pins provider=ridge AND its model). Passing a bare llm=ridge with no
    model would inherit DS4's model id from the pi-coding profile — a session
    pointed at Ridge asking for a model Ridge does not serve.
    """

    tier: str                       # charter vocabulary: local|ridge|red|cloud
    strength: int                   # strictly increasing; the only ordering
    label: str
    backend: Optional[str] = None
    model: Optional[str] = None
    llm: Optional[str] = None
    profile: Optional[str] = None   # db.agents slug

    def start_kwargs(self) -> dict:
        """The subset of `start_session()` kwargs this rung pins."""
        kwargs: dict = {}
        if self.backend:
            kwargs["backend"] = self.backend
        if self.model:
            kwargs["model"] = self.model
        if self.llm:
            kwargs["llm"] = self.llm
        if self.profile:
            kwargs["subagent_profile"] = self.profile
        return kwargs


def default_ladder() -> list[Rung]:
    """The ladder, weakest first. Built per call so a settings change (e.g. a
    new Sonnet/Opus id) is picked up without a process restart."""
    return [
        # Local: no model pinned — start_session fills provider+model from the
        # pi-coding profile, which is the box's single DS4 slot.
        Rung(tier=TIER_LOCAL, strength=0, label="pi on DS4 (local)",
             backend="pi-code"),
        Rung(tier=TIER_RIDGE, strength=1, label="pi on Ridge (Qwen, WoL)",
             backend="pi-code", profile="pi-coding-ridge"),
        Rung(tier=TIER_RED, strength=2, label="pi on RED (Qwen, WoL)",
             backend="pi-code", profile="pi-coding-red"),
        Rung(tier=TIER_CLOUD, strength=3, label="claude_code (standard tier)",
             backend=CLAUDE_PROVIDER, model=settings.coding_routing_model_standard),
        Rung(tier=TIER_CLOUD, strength=4, label="claude_code (deep tier)",
             backend=CLAUDE_PROVIDER, model=settings.coding_routing_model_deep),
    ]


@dataclass
class RerouteVerdict:
    """The decision to re-run a failed session on a stronger tier."""

    rung: Rung
    from_tier: str
    from_strength: int
    why: str
    attempt: int                     # 1 = first re-route for this session
    skipped: list[str] = field(default_factory=list)

    @property
    def tier(self) -> str:
        return self.rung.tier

    def start_kwargs(self) -> dict:
        return self.rung.start_kwargs()

    def to_dict(self) -> dict:
        return {
            "tier": self.rung.tier,
            "label": self.rung.label,
            "strength": self.rung.strength,
            "backend": self.rung.backend,
            "model": self.rung.model,
            "llm": self.rung.llm,
            "profile": self.rung.profile,
            "from_tier": self.from_tier,
            "from_strength": self.from_strength,
            "attempt": self.attempt,
            "why": self.why,
            "skipped": list(self.skipped),
            "decided_at": datetime.now(timezone.utc),
        }


def classify_tier(session: Optional[dict]) -> str:
    """Which charter tier a coding session actually ran on.

    Reads the session doc rather than its routing verdict: a session can be
    re-pointed after routing (profile resolution, a caller-pinned model), and
    the tier that matters for escalation is the one that just failed.
    """
    session = session or {}
    backend = (session.get("backend") or "").strip().lower()
    llm = (session.get("llm") or "").strip().lower()
    model = (session.get("model") or "").strip().lower()

    # codex is a cloud tier even though it is not Anthropic: it runs against a
    # hosted API, not a slot on this box. Classifying it as local would make the
    # ladder "promote" a failed codex session onto Ridge — a demotion dressed as
    # an escalation.
    if backend in ("claude_code", "claude-code", "codex"):
        return TIER_CLOUD
    if llm in ("ridge", "ridge-proxy"):
        return TIER_RIDGE
    if llm in ("red", "red-proxy"):
        return TIER_RED
    if "ridge" in model:
        return TIER_RIDGE
    if "red-" in model or model.startswith("red"):
        return TIER_RED
    if backend in ("pi-code", "pool") or llm:
        return TIER_LOCAL
    if model.startswith("claude-"):
        return TIER_CLOUD
    return TIER_LOCAL


def _current_strength(session: Optional[dict], ladder: list[Rung]) -> int:
    """Strength of the rung the session ran on. For the cloud tier the model id
    disambiguates standard from deep, so a failed Opus run is not 'promoted'
    back to Sonnet."""
    tier = classify_tier(session)
    model = ((session or {}).get("model") or "").strip().lower()
    matches = [r for r in ladder if r.tier == tier]
    if not matches:
        return -1
    exact = [r for r in matches if r.model and r.model.lower() == model]
    if exact:
        return max(r.strength for r in exact)
    # Unknown model on a known tier: assume the weakest rung of that tier, so
    # the ladder still has somewhere to go rather than parking immediately.
    return min(r.strength for r in matches)


def _tiers_from_charter(charter) -> list[str]:
    """`tiers_allowed` out of a Charter model, a dict, or a bare list."""
    if charter is None:
        return []
    if isinstance(charter, (list, tuple, set)):
        return [str(t).strip().lower() for t in charter if t]
    tiers = None
    if isinstance(charter, dict):
        tiers = charter.get("tiers_allowed")
    else:
        tiers = getattr(charter, "tiers_allowed", None)
    return [str(t).strip().lower() for t in (tiers or []) if t]


async def _profile_exists(db, slug: str) -> bool:
    """A ladder rung whose launch profile is not in db.agents is not a rung.

    `pi-coding-red` in particular does not exist on this box today; escalating
    into it would raise "subagent profile not found" out of start_session and
    turn a recoverable stall into a hard failure.
    """
    if db is None:
        return False
    try:
        doc = await db.agents.find_one({"slug": slug})
    except Exception as exc:  # noqa: BLE001 — availability check, never fatal
        logger.debug("reroute: could not verify profile %s: %s", slug, exc)
        return False
    if not doc:
        return False
    if doc.get("enabled") is False:
        return False
    return True


async def reroute(
    db: Optional[AsyncIOMotorDatabase],
    session: dict,
    *,
    charter=None,
    tiers_allowed: Optional[list[str]] = None,
    previous_tiers: Optional[list[str]] = None,
    ladder: Optional[list[Rung]] = None,
    reason: str = "",
) -> Optional[RerouteVerdict]:
    """Pick the next stronger tier for a failed session, or None.

    None means "the ladder is exhausted here" — every stronger rung is either
    outside the charter, already tried, or unavailable. The caller escalates
    (L4/L5); it must NOT re-run the same tier, which is what the ladder exists
    to stop.

    Never raises: an unreachable db degrades to "that rung is unavailable",
    because refusing to escalate is always safe and escalating into a profile
    that isn't there is not.
    """
    ladder = ladder or default_ladder()
    ladder = sorted(ladder, key=lambda r: r.strength)

    from_tier = classify_tier(session)
    from_strength = _current_strength(session, ladder)

    allowed = [t for t in (tiers_allowed or _tiers_from_charter(charter))]
    # An unset `tiers_allowed` is unset, not empty: a charter that never named
    # its tiers has not restricted anything, and treating it as "nothing is
    # allowed" would silently disable L3 for every project Ben hasn't finished
    # filling in. The real caps are autonomy level and the merge gate.
    unrestricted = not allowed

    tried = {str(t).strip().lower() for t in (previous_tiers or [])}
    tried |= {
        str(entry.get("tier") or "").strip().lower()
        for entry in ((session.get("reroute") or {}).get("history") or [])
        if isinstance(entry, dict)
    }

    cloud_cooling: Optional[datetime] = None
    if db is not None:
        try:
            cloud_cooling = await get_cooldown(db, CLAUDE_PROVIDER)
        except Exception as exc:  # noqa: BLE001
            logger.debug("reroute: availability check failed: %s", exc)

    skipped: list[str] = []
    for rung in ladder:
        if rung.strength <= from_strength:
            continue
        if not unrestricted and rung.tier not in allowed:
            skipped.append(f"{rung.label}: not in charter tiers_allowed")
            continue
        if rung.tier in tried:
            skipped.append(f"{rung.label}: tier already tried for this session")
            continue
        if rung.tier == TIER_CLOUD and cloud_cooling is not None:
            mins = max(1, int((cloud_cooling - datetime.now(timezone.utc)).total_seconds() // 60))
            skipped.append(f"{rung.label}: Claude quota cooling down ~{mins}m")
            continue
        if rung.profile and not await _profile_exists(db, rung.profile):
            skipped.append(f"{rung.label}: launch profile '{rung.profile}' not available")
            continue

        why = (
            f"{from_tier} → {rung.tier}: {reason or 'previous attempt failed'}"
        )
        return RerouteVerdict(
            rung=rung,
            from_tier=from_tier,
            from_strength=from_strength,
            why=why,
            attempt=len([t for t in tried if t]) + 1,
            skipped=skipped,
        )

    logger.info(
        "reroute: no stronger tier available for session %s (from %s): %s",
        session.get("_id"), from_tier, "; ".join(skipped) or "ladder exhausted",
    )
    return None


# How much of the failure history to carry. A re-route whose prompt is mostly
# the previous agent's output is a re-route that spends its context on the
# failure instead of the task.
REROUTE_HISTORY_CHARS = 4000


def build_reroute_prompt(
    original_prompt: str,
    history: Optional[list[dict]] = None,
    *,
    verdict: Optional[RerouteVerdict] = None,
    max_chars: int = REROUTE_HISTORY_CHARS,
) -> str:
    """Original task + a Reflexion-style note on what already failed.

    `history` entries are `{"tier"/"label", "outcome"/"reason", "evidence"}`
    dicts — whatever the supervisor recorded per attempt. A re-route without
    this note is just the same task on a bigger model, which is the version of
    L3 that reliably reproduces the original failure.
    """
    lines: list[str] = []
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        who = entry.get("label") or entry.get("tier") or entry.get("model") or "previous attempt"
        outcome = entry.get("reason") or entry.get("outcome") or "failed"
        line = f"- {who}: {outcome}"
        evidence = (entry.get("evidence") or "").strip()
        if evidence:
            line += f"\n  evidence: {evidence[:600]}"
        lines.append(line)

    if not lines:
        return original_prompt

    note = "\n".join(lines)
    if len(note) > max_chars:
        note = note[:max_chars] + "\n  … (history truncated)"

    header = "A previous attempt at this task failed. What was tried:"
    footer = (
        "Do not repeat the failed approach. Start by establishing what is "
        "actually true in the workspace (read the files, run the check) before "
        "changing anything."
    )
    if verdict is not None:
        header = (
            f"This task is being retried on a stronger tier "
            f"({verdict.from_tier} → {verdict.tier}). What was tried before:"
        )
    return f"{original_prompt}\n\n---\n{header}\n{note}\n\n{footer}"
