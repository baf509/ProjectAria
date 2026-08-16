"""
ARIA - Steward Research Planner

Purpose: turn a project's charter into proactive, deduplicated, budgeted
research that ARIA runs on its own — and refuse to publish what it cannot
verify.

Design: /home/ben/Obsidian/vault/ProjectAria/Planning/ARIA_PROJECT_STEWARD_PROPOSAL_20260815.md §5

The substrate for this already existed and had never fired once: `research_runs`
was 0, `db.schedules` was empty, and nothing anywhere called `start_research`.
The missing parts were never the search-fetch-learn loop; they were the five
things this module owns:

1. QUESTIONS — derived from the charter purpose/topics, the project's open
   next_steps and tasks, the last 14 days of `machine_scan` memories for the
   repo, and the dream cycle's `knowledge_gaps` (which have been stored nightly
   and acted on by nothing).
2. DEDUP + COOL-DOWN — a stable `topic_hash` per question, checked against
   prior runs and research-sourced memories. The existing dedup is intra-run
   exact-string equality, which cannot notice that yesterday's run asked the
   same thing in different words.
3. BUDGET — `research_runs_per_week` from the charter's effective budget, plus
   wall-clock, token and source caps that are ENFORCED. `total_tokens` is
   summed by ResearchService today and never checked against anything.
4. CITATION CHECK — after a run, every claimed source_url is re-fetched and the
   claim must actually appear on the page. Local models fabricate URLs and
   quotes, and an unverified citation is worse than no citation because it
   looks like evidence. A note whose sources verify at zero is kept as memory
   but NOT published.
5. PUBLISH — into `vault/<Project>/Research/` with `accepted: pending` and the
   `topic_hash` in frontmatter, so Ben's phone edit of `accepted:` is what
   grades this loop (VaultReader reads it back). Volume is not the metric.

What this module deliberately does NOT do: talk to Ben. Every alert it raises
is `needs_human=False` — a finished research note is cockpit and digest
material. Research must never be able to page anyone, or the first week of
nightly runs teaches Ben to ignore the channel that carries real escalations.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from aria.config import settings
from aria.llm.base import Message
from aria.llm.manager import llm_manager
from aria.planning.models import Project
from aria.planning.service import PlanningService, effective_budget
from aria.tools.builtin.web import WebTool

logger = logging.getLogger(__name__)

SOURCE = "research_planner"


def _setting(name: str, default):
    """Read a planner setting with a fallback.

    Every knob below belongs in `aria/config.py` next to the other steward
    settings, but config.py is a shared hub this module is not allowed to edit
    (see the INTEGRATION SPEC in the handoff). `getattr` means the planner runs
    with the documented defaults today and picks up the real settings the
    moment they land, without a second edit here.
    """
    return getattr(settings, name, default)


# --------------------------------------------------------------------------
# Text normalisation — the basis of the topic hash and the citation check
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_QUOTED = re.compile(r"[\"“”'‘’]([^\"“”\n]{12,200})[\"“”'‘’]")
_NUMBER = re.compile(r"\d[\d,.]*")

# Words that carry no topic identity. Kept small on purpose: the hash only has
# to be stable across capitalisation, punctuation and filler, not to do IR.
_STOPWORDS = frozenset(
    """a an and are as at be been but by can could do does for from has have how
    in into is it its may might not of on or should so than that the their then
    there these this those to use used using was were what when where which who
    why will with would you your about across best current currently new latest""".split()
)


def normalize_question(text: str) -> str:
    """Lowercase, strip punctuation, drop filler, sort nothing.

    Word ORDER is kept: "rocm on windows" and "windows on rocm" are different
    questions. Only surface noise is removed, which is what makes the hash
    stable when the same question comes back capitalised differently or with a
    trailing question mark.
    """
    lowered = _NON_WORD.sub(" ", (text or "").lower())
    words = [w for w in lowered.split() if w and w not in _STOPWORDS]
    return " ".join(words)


def topic_hash(text: str) -> str:
    """Stable identity for a research question. 16 hex chars is plenty for a
    per-project topic space and stays readable in frontmatter."""
    return hashlib.sha256(normalize_question(text).encode("utf-8")).hexdigest()[:16]


def strip_html(value: str) -> str:
    """Text of a fetched page, good enough to look for a quote in.

    Deliberately local rather than reusing ResearchService._strip_html: that is
    a private method on a class another agent is editing, and the citation
    check must not break because a private helper was renamed.
    """
    text = _SCRIPT_STYLE.sub(" ", value or "")
    text = _TAG.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return _WS.sub(" ", text).strip()


def _distinctive_tokens(text: str) -> list[str]:
    """Tokens that a page must contain for a claim to be considered supported:
    words of 4+ characters that are not filler, plus every number. Numbers are
    included because a fabricated benchmark figure is the single most damaging
    thing a local model puts in a research note."""
    words = [w for w in normalize_question(text).split() if len(w) >= 4]
    numbers = [n.replace(",", "") for n in _NUMBER.findall(text or "")]
    return words + numbers


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One proposed research question plus where it came from. `origin` is kept
    because it is the only way to tell later whether the model's own questions
    or the charter's seeds produce the notes Ben marks `accepted: true`."""

    question: str
    origin: str
    rationale: str = ""

    @property
    def topic_hash(self) -> str:
        return topic_hash(self.question)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "topic_hash": self.topic_hash,
            "origin": self.origin,
            "rationale": self.rationale,
        }


class EmptyCompletion(RuntimeError):
    """The local model returned no content.

    Qwen3.8 is a REASONING model: it emits `reasoning_content` first, so a tight
    max_tokens comes back with finish_reason="length" and content="". Treating
    that as a valid answer is exactly how DS4 silently labelled every memory
    with zero entities — so an empty completion is an error here, never a
    result.
    """


QUESTION_PROMPT = """You are ARIA's research planner for a single software project.

PROJECT: {name}
PURPOSE: {purpose}
GOALS:
{goals}
NON-GOALS (never research these):
{non_goals}
CHARTER RESEARCH TOPICS:
{topics}
OPEN NEXT STEPS AND TASKS:
{tasks}
RECENT MACHINE ACTIVITY (last {scan_days} days):
{scan}
KNOWN KNOWLEDGE GAPS:
{gaps}
ALREADY RESEARCHED RECENTLY (do not repeat these):
{recent}

Write {want} research questions that would materially help this project.
Rules:
- Each question must be answerable from public web sources.
- Each must be specific enough to search: name the technology, version or
  constraint. "How do we improve performance?" is useless.
- Do not restate a question from ALREADY RESEARCHED.
- Do not propose anything under NON-GOALS.

Return ONLY a JSON array, no prose, no code fence:
[{{"question": "...", "why": "one short sentence"}}]"""


class ResearchPlanner:
    """Per-project research: what to ask, whether we may ask it, and whether
    the answer is trustworthy enough to publish.

    Collaborators are injected rather than resolved so this is unit-testable
    without Mongo, a model server or the network:
      `research`  - aria.research.service.ResearchService (called as-is)
      `planning`  - aria.planning.service.PlanningService
      `notifier`  - aria.notifications.service.NotificationService
      `writer`    - aria.integrations.obsidian.ObsidianWriter
      `web`       - aria.tools.builtin.web.WebTool (SSRF-guarded already)
    """

    def __init__(
        self,
        db,
        *,
        research=None,
        planning: Optional[PlanningService] = None,
        notifier=None,
        writer=None,
        web=None,
        poll_seconds: float = 5.0,
    ):
        self.db = db
        self.research = research
        self.planning = planning or PlanningService(db)
        self.notifier = notifier
        self._writer = writer
        self.web = web or WebTool(timeout_seconds=20, max_response_size=512 * 1024)
        self.poll_seconds = poll_seconds
        # Pinned, never the /llm/v1 auto-route: that resolves to the largest
        # resident model, which is DS4 — pi's SINGLE coding slot. A research
        # prefill there evicts a coding agent's warm prefix (4.2 s warm vs
        # 39.5 s cold). Research runs on Qwen slot 2 or it does not run.
        self.backend = _setting("steward_backend", "llamacpp")
        self.model = _setting("steward_model", "qwen3.8-27b-rocmfp4-r9700")
        self.endpoint = _setting("steward_endpoint", "http://127.0.0.1:8080/v1")
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # Minutes since the last sign of Hermes activity, refreshed once per
        # run. None means "no evidence", which is treated as busy.
        self._hermes_idle_cached: Optional[float] = None

    # ---------------------------------------------------------------- worker

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="steward.research")
        logger.info(
            "research planner started (every %d min, model=%s @ %s)",
            int(_setting("research_planner_interval_minutes", 360)),
            self.model,
            self.endpoint,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        interval = max(60, int(_setting("research_planner_interval_minutes", 360)) * 60)
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("research planner tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ tick

    async def tick(self) -> dict:
        """One pass over the active set. Runs AT MOST
        `research_planner_max_projects_per_tick` projects (default 1): there is
        exactly one background local slot on this box, so a tick that fires
        research for four projects at once is four runs queued behind each
        other with their wall-clock budgets already burning."""
        results: list[dict] = []
        max_projects = int(_setting("research_planner_max_projects_per_tick", 1))
        try:
            projects = await self.planning.active_projects()
        except Exception:
            logger.exception("research planner: cannot read the active set")
            return {"ran": 0, "results": []}

        for project in projects:
            if len(results) >= max_projects:
                break
            due, reason = await self._is_due(project)
            if not due:
                results.append({"project": project.slug, "status": "skipped", "reason": reason})
                continue
            results.append(await self.run_project(project))
        ran = sum(1 for r in results if r.get("status") not in {"skipped", "error"})
        return {"ran": ran, "results": results}

    async def _plan_approval(self, project: Project) -> Optional[str]:
        """`approval:` from the project's STEWARD_PLAN.md, or None.

        Deliberately the file and not the Mongo mirror — the vault is the
        approval surface, and gating on a remembered copy would run research Ben
        had since revoked. Any failure to read or parse returns None, which the
        caller treats as "not approved".
        """
        try:
            from aria.integrations.obsidian import parse_frontmatter

            path = self._plan_path(project)
            if path is None or not path.exists():
                return None
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            value = fm.get("approval")
            return value.strip().lower() if isinstance(value, str) else None
        except Exception as exc:  # noqa: BLE001 — unreadable is not approved
            logger.debug(
                "research planner: plan approval unreadable for %s (%s)",
                project.slug, exc,
            )
            return None

    def _plan_path(self, project: Project):
        """Where this project's STEWARD_PLAN.md lives — resolved through the
        writer's own folder rule, so we read the file the steward writes."""
        try:
            folder_for = getattr(self.writer, "_folder_for", None)
            if callable(folder_for):
                return folder_for(project.path or project.slug) / "Planning" / "STEWARD_PLAN.md"
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _is_due(self, project: Project) -> tuple[bool, str]:
        charter = project.charter
        cadence = (charter.cadence.research if charter and charter.cadence else "weekly") or "weekly"
        if cadence.strip().lower() == "manual":
            return False, "cadence_manual"
        # BEN MUST HAVE APPROVED THE PLAN. Research reaches the public internet,
        # spends a token budget and publishes into his vault — it is an outward
        # action, not a proposal, and he asked for it to wait on his review
        # (2026-08-15). Read from the plan FILE, the same authority the steward
        # uses, because an approval ARIA merely remembers is not an approval.
        # Fails closed: pending, rejected, missing, unreadable and unparseable
        # all mean "not approved".
        approval = await self._plan_approval(project)
        if approval != "approved":
            return False, f"plan_not_approved:{approval or 'pending'}"
        if project.steward and project.steward.paused_reason:
            # The steward has stood down on this project pending Ben's decision;
            # spending its research budget in the meantime is exactly the kind
            # of "kept working while asking to stop" that makes the pause
            # proposal meaningless.
            return False, "steward_paused"
        last = await self._last_run_at(project.slug)
        if last is None:
            return True, "never_run"
        gap = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}.get(
            cadence.strip().lower(), timedelta(days=7)
        )
        if _utcnow() - last < gap:
            return False, "cadence_not_due"
        return True, "due"

    # ------------------------------------------------------- 1. question gen

    async def generate_questions(self, project: Project, *, want: int = 0) -> list[Candidate]:
        """3-5 candidate questions from the charter and the project's own
        signals. Falls back to deterministic seeds when the model is
        unavailable or returns nothing: a planner that produces zero questions
        because a model server was restarting is indistinguishable from a
        planner that has nothing to ask, and the second is a decision while the
        first is an outage."""
        want = want or int(_setting("research_planner_max_questions", 5))
        context = await self._gather_context(project)
        seeds = self._seed_candidates(project, context, want)

        prompt = self._build_prompt(project, context, want)
        try:
            raw = await self._complete(prompt, max_tokens=int(_setting("steward_max_tokens", 2048)))
        except Exception as exc:
            logger.warning(
                "research planner: question generation failed for %s (%s); using %d charter seeds",
                project.slug, exc, len(seeds),
            )
            await self._log_event(project, "questions_fallback", str(exc)[:300])
            return seeds

        parsed = _parse_json_array(raw)
        model_candidates: list[Candidate] = []
        for item in parsed:
            if isinstance(item, str):
                question, why = item.strip(), ""
            elif isinstance(item, dict):
                question = str(item.get("question") or "").strip()
                why = str(item.get("why") or "").strip()
            else:
                continue
            if len(question) < 12:
                continue
            model_candidates.append(Candidate(question=question, origin="model", rationale=why))

        if not model_candidates:
            # Parsed to nothing: the model spoke but said nothing usable. Same
            # rule as an empty completion — never write a zero result.
            logger.warning(
                "research planner: unusable question JSON for %s (%.200s)", project.slug, raw
            )
            await self._log_event(project, "questions_unparseable", raw[:300])
            return seeds

        merged = _dedupe_candidates([*model_candidates, *seeds])
        return merged[:want]

    def _build_prompt(self, project: Project, context: dict, want: int) -> str:
        prompt = QUESTION_PROMPT.format(
            name=project.name,
            purpose=(project.charter.purpose if project.charter else "") or "(none)",
            goals=_bullets(context["goals"]),
            non_goals=_bullets(context["non_goals"]),
            topics=_bullets(context["topics"]),
            tasks=_bullets(context["tasks"]),
            scan=_bullets(context["scan"]),
            gaps=_bullets(context["gaps"]),
            recent=_bullets(context["recent_questions"]),
            scan_days=int(_setting("research_planner_scan_window_days", 14)),
            want=want,
        )
        # Prompt size is a scheduling decision, not a formatting one: a 13K cold
        # prefill on Qwen slot 2 drags slot 1 (Hermes) to ~6 t/s, so by day the
        # prompt is cut to ~6-8K tokens' worth of characters. At night, or when
        # Hermes has been idle, the full context goes in.
        if not self._heavy_allowed()[0]:
            limit = int(_setting("research_planner_day_prompt_tokens", 6000)) * 4
            if len(prompt) > limit:
                prompt = prompt[:limit] + "\n\n(context truncated for daytime slot sharing)\n"
        return prompt

    def _seed_candidates(self, project: Project, context: dict, want: int) -> list[Candidate]:
        seeds: list[Candidate] = []
        for topic in context["topics"]:
            seeds.append(Candidate(question=topic, origin="charter_topic",
                                   rationale="charter research topic"))
        for gap in context["gaps"]:
            seeds.append(Candidate(question=gap, origin="knowledge_gap",
                                   rationale="dream cycle knowledge gap"))
        for step in context["next_steps"]:
            seeds.append(Candidate(
                question=f"What is the current best approach to: {step}",
                origin="next_step", rationale="open next step",
            ))
        if not seeds and project.charter and project.charter.purpose.strip():
            seeds.append(Candidate(
                question=f"What is the current state of the art relevant to: {project.charter.purpose.strip()}",
                origin="purpose", rationale="charter purpose",
            ))
        return _dedupe_candidates(seeds)[:want]

    async def _gather_context(self, project: Project) -> dict:
        charter = project.charter
        scan_days = int(_setting("research_planner_scan_window_days", 14))
        return {
            "goals": list(charter.goals) if charter else [],
            "non_goals": list(charter.non_goals) if charter else [],
            "topics": [t.strip() for t in (charter.research_topics if charter else []) if t.strip()],
            "next_steps": [s.strip() for s in (project.next_steps or []) if s.strip()],
            "tasks": await self._open_tasks(project),
            "scan": await self._scan_memories(project, scan_days),
            "gaps": await self._knowledge_gaps(project),
            "recent_questions": await self._recent_questions(project),
        }

    async def _open_tasks(self, project: Project) -> list[str]:
        out = [f"next step: {s}" for s in (project.next_steps or []) if s.strip()]
        try:
            docs = await self.db.tasks.find(
                {"project_id": project.id, "status": {"$in": ["proposed", "active"]}}
            ).sort("updated_at", -1).to_list(length=10)
        except Exception:
            logger.debug("research planner: task read failed", exc_info=True)
            docs = []
        out.extend(str(d.get("title") or "").strip() for d in docs if d.get("title"))
        return [t for t in out if t][:12]

    async def _scan_memories(self, project: Project, days: int) -> list[str]:
        """Machine-scan memories for THIS repo. Matching is done in Python
        because the emitters disagree about where the repo identity lives:
        GitChangeEmitter puts the path in `source.repo` and the repo name in
        `categories`, the container/service emitter records neither."""
        cutoff = _utcnow() - timedelta(days=days)
        try:
            docs = await self.db.memories.find(
                {"source.type": "machine_scan", "created_at": {"$gte": cutoff}}
            ).sort("created_at", -1).to_list(length=80)
        except Exception:
            logger.debug("research planner: memory read failed", exc_info=True)
            return []
        path = (project.path or "").rstrip("/")
        name = (path.rsplit("/", 1)[-1] if path else project.name) or project.slug
        needles = {n.lower() for n in (name, project.slug, project.name) if n}
        out: list[str] = []
        for doc in docs:
            source = doc.get("source") or {}
            repo = str(source.get("repo") or "").rstrip("/")
            cats = {str(c).lower() for c in (doc.get("categories") or [])}
            content = str(doc.get("content") or "")
            if (path and repo == path) or (needles & cats) or any(n in content.lower() for n in needles):
                out.append(content.strip())
        return out[:15]

    async def _knowledge_gaps(self, project: Project) -> list[str]:
        """The dream cycle has been writing `knowledge_gaps` into
        `dream_journal` nightly and nothing has ever read them. They are
        machine-wide, so only gaps that share a distinctive word with this
        project's purpose/topics are offered to it."""
        cutoff = _utcnow() - timedelta(days=int(_setting("research_planner_gap_window_days", 30)))
        try:
            docs = await self.db.dream_journal.find(
                {"created_at": {"$gte": cutoff}}
            ).sort("created_at", -1).to_list(length=10)
        except Exception:
            logger.debug("research planner: dream_journal read failed", exc_info=True)
            return []
        charter = project.charter
        vocabulary = set(_distinctive_tokens(
            " ".join([
                project.name, project.slug,
                charter.purpose if charter else "",
                " ".join(charter.research_topics) if charter else "",
                " ".join(charter.goals) if charter else "",
            ])
        ))
        out: list[str] = []
        for doc in docs:
            for gap in doc.get("knowledge_gaps") or []:
                text = gap if isinstance(gap, str) else str(gap.get("gap") or gap.get("question") or "")
                text = text.strip()
                if len(text) < 12:
                    continue
                if vocabulary and not (set(_distinctive_tokens(text)) & vocabulary):
                    continue
                out.append(text)
        return out[:8]

    async def _recent_questions(self, project: Project) -> list[str]:
        seen = await self._seen_topics(project)
        return [entry["question"] for entry in seen.values() if entry.get("question")][:15]

    # ------------------------------------------------------ 2. dedup/cooldown

    async def _seen_topics(self, project: Project) -> dict[str, dict]:
        """topic_hash -> {at, question, via} for everything researched inside
        the cool-down window.

        Two sources, because a run and a memory can each outlive the other:
        `research_runs` (annotated by this planner, or hashed from the raw query
        for runs started by hand) and research-sourced memories.
        """
        days = int(_setting("research_topic_cooldown_days", 30))
        cutoff = _utcnow() - timedelta(days=days)
        seen: dict[str, dict] = {}

        def _record(thash: str, at, question: str, via: str) -> None:
            if not thash:
                return
            at = _as_utc(at)
            prior = seen.get(thash)
            if prior is None or (at and (prior.get("at") is None or at > prior["at"])):
                seen[thash] = {"at": at, "question": question, "via": via}

        try:
            runs = await self.db.research_runs.find(
                {"created_at": {"$gte": cutoff}}
            ).to_list(length=200)
        except Exception:
            logger.debug("research planner: research_runs read failed", exc_info=True)
            runs = []
        for run in runs:
            planner = run.get("planner") or {}
            slug = planner.get("project_slug")
            # A run for another project does not consume this project's
            # cool-down: two projects legitimately research the same subject
            # from different angles, and cross-blocking them would silently
            # starve whichever one ticks second.
            if slug and slug != project.slug:
                continue
            query = run.get("query") or ""
            _record(planner.get("topic_hash") or topic_hash(query), run.get("created_at"), query, "run")

        try:
            memories = await self.db.memories.find(
                {"source.type": {"$in": ["research", "research_report"]},
                 "created_at": {"$gte": cutoff}}
            ).to_list(length=200)
        except Exception:
            logger.debug("research planner: research memory read failed", exc_info=True)
            memories = []
        for mem in memories:
            source = mem.get("source") or {}
            query = str(source.get("query") or "")
            thash = source.get("topic_hash") or (topic_hash(query) if query else "")
            _record(thash, mem.get("created_at"), query, "memory")

        return seen

    async def filter_candidates(
        self, project: Project, candidates: list[Candidate]
    ) -> tuple[list[Candidate], list[dict]]:
        """Drop candidates inside the cool-down. A charter amended AFTER the
        prior run waives it: Ben rewriting the purpose is a statement that the
        old answer was researched against the wrong question."""
        seen = await self._seen_topics(project)
        changed_at = await self._charter_changed_at(project)
        accepted: list[Candidate] = []
        rejected: list[dict] = []
        batch: set[str] = set()
        for cand in candidates:
            thash = cand.topic_hash
            if thash in batch:
                rejected.append({**cand.to_dict(), "reason": "duplicate_in_batch"})
                continue
            prior = seen.get(thash)
            if prior:
                prior_at = prior.get("at")
                if changed_at and prior_at and changed_at > prior_at:
                    logger.info(
                        "research planner: %s cool-down waived for %s (charter amended %s)",
                        project.slug, thash, changed_at.isoformat(),
                    )
                else:
                    rejected.append({**cand.to_dict(), "reason": f"cooldown:{prior.get('via')}"})
                    continue
            batch.add(thash)
            accepted.append(cand)
        return accepted, rejected

    async def _charter_changed_at(self, project: Project) -> Optional[datetime]:
        charter = project.charter
        if charter and charter.approved_at:
            return _as_utc(charter.approved_at)
        # `set_charter` stamps source.charter.at on the raw document for every
        # write, including the ones a partial vault edit makes without touching
        # approved_at — so that is the more reliable "when did the charter last
        # change" marker.
        try:
            doc = await self.db.projects.find_one({"slug": project.slug})
        except Exception:
            return None
        stamp = (((doc or {}).get("source") or {}).get("charter") or {}).get("at")
        return _as_utc(stamp)

    # -------------------------------------------------------------- 3. budget

    async def budget_state(self, project: Project) -> dict:
        budget = effective_budget(project.charter)
        cap = budget.get("research_runs_per_week")
        week_ago = _utcnow() - timedelta(days=7)
        try:
            used = await self.db.research_runs.count_documents(
                {"planner.project_slug": project.slug, "created_at": {"$gte": week_ago}}
            )
        except Exception:
            logger.debug("research planner: run count failed", exc_info=True)
            used = 0
        return {
            "runs_per_week": cap,
            "runs_used": int(used),
            "exhausted": cap is not None and int(used) >= int(cap),
            "max_tokens": int(_setting("research_planner_max_tokens_per_run", 60000)),
            "max_wall_minutes": int(_setting("research_planner_max_wall_minutes", 20)),
            "max_sources": int(_setting("research_planner_max_sources", 12)),
        }

    async def _last_run_at(self, slug: str) -> Optional[datetime]:
        try:
            docs = await self.db.research_runs.find(
                {"planner.project_slug": slug}
            ).sort("created_at", -1).limit(1).to_list(length=1)
        except Exception:
            return None
        return _as_utc(docs[0].get("created_at")) if docs else None

    # ---------------------------------------------------------- 5. scheduling

    def _heavy_allowed(self, now: Optional[datetime] = None) -> tuple[bool, str]:
        """May this run take the big prefill?

        Night window (01:00-07:00 local) is unconstrained. Outside it, only when
        Hermes has been idle — the two share Qwen, and slot 1 is Ben's
        conversation. `_hermes_idle_minutes` returning None means "no evidence
        either way", which is treated as BUSY: guessing wrong toward heavy
        degrades the channel Ben is actually talking on.
        """
        now = (now or datetime.now()).astimezone()
        start = int(_setting("research_planner_night_start_hour", 1))
        end = int(_setting("research_planner_night_end_hour", 7))
        if start <= now.hour < end:
            return True, "night_window"
        idle = self._hermes_idle_cached
        threshold = int(_setting("research_planner_hermes_idle_minutes", 10))
        if idle is not None and idle >= threshold:
            return True, f"hermes_idle_{int(idle)}m"
        return False, "daytime_shared_slot"

    async def _refresh_hermes_idle(self) -> Optional[float]:
        """Minutes since the last sign of conversational activity, or None when
        there is no evidence.

        Hermes reaches ARIA only through the MCP server, so its footprint here
        is conversation messages and the alert relay's own acks. This is a
        proxy, not a measurement of Qwen slot 1 — the honest fix is a slot-level
        reading from the model server, which does not exist yet (HOUSE_AGENT
        P.4 still owes the 1-slot/2-slot curve).
        """
        stamps: list[datetime] = []
        for collection, field in (("messages", "created_at"), ("alerts", "delivered_at")):
            try:
                docs = await self.db[collection].find(
                    {field: {"$ne": None}}
                ).sort(field, -1).limit(1).to_list(length=1)
            except Exception:
                continue
            if docs:
                stamp = _as_utc(docs[0].get(field))
                if stamp:
                    stamps.append(stamp)
        if not stamps:
            self._hermes_idle_cached = None
            return None
        self._hermes_idle_cached = (_utcnow() - max(stamps)).total_seconds() / 60.0
        return self._hermes_idle_cached

    def _run_shape(self, heavy: bool, budget: dict) -> tuple[int, int]:
        """(depth, breadth) — the only lever the planner has over how much of
        the box a run consumes, since ResearchService fans out depth*breadth
        fetches. Kept under `max_sources` in both modes."""
        if heavy:
            depth = int(_setting("research_planner_night_depth", 2))
            breadth = int(_setting("research_planner_night_breadth", 4))
        else:
            depth = int(_setting("research_planner_day_depth", 1))
            breadth = int(_setting("research_planner_day_breadth", 3))
        max_sources = max(1, int(budget.get("max_sources") or 12))
        while depth * breadth > max_sources and breadth > 1:
            breadth -= 1
        return max(1, depth), max(1, breadth)

    # -------------------------------------------------------------- the run

    async def run_project(
        self,
        project: Project | str,
        *,
        questions: Optional[list[str]] = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Plan and execute one research run for a project. Returns a result
        dict; never raises into a scheduler tick or a worker loop."""
        try:
            proj = await self._resolve_project(project)
        except Exception as exc:
            logger.exception("research planner: project resolution failed")
            return {"status": "error", "reason": str(exc)[:200]}
        if proj is None:
            return {"status": "error", "reason": "project_not_found",
                    "project": project if isinstance(project, str) else None}
        if not (proj.charter and proj.charter.purpose.strip()):
            # Without a purpose there is nothing to derive a question FROM, and
            # a generic question is how a research loop produces busywork.
            return {"status": "skipped", "project": proj.slug, "reason": "no_charter_purpose"}

        result: dict = {"project": proj.slug, "status": "skipped"}
        if not force:
            due, reason = await self._is_due(proj)
            if not due and reason in {"cadence_manual", "steward_paused"}:
                return {**result, "reason": reason}

        budget = await self.budget_state(proj)
        if budget["exhausted"] and not force:
            return {**result, "reason": "budget_exhausted", "budget": budget}

        await self._refresh_hermes_idle()
        candidates = (
            [Candidate(question=q, origin="explicit") for q in questions]
            if questions else await self.generate_questions(proj)
        )
        accepted, rejected = await self.filter_candidates(proj, candidates)
        if not accepted:
            return {**result, "reason": "no_new_topics",
                    "rejected": rejected, "considered": len(candidates)}

        chosen = accepted[0]
        heavy, heavy_reason = self._heavy_allowed()
        depth, breadth = self._run_shape(heavy, budget)
        plan = {
            "project": proj.slug,
            "question": chosen.question,
            "topic_hash": chosen.topic_hash,
            "origin": chosen.origin,
            "depth": depth,
            "breadth": breadth,
            "heavy": heavy,
            "mode": heavy_reason,
            "budget": budget,
            "alternates": [c.to_dict() for c in accepted[1:]],
            "rejected": rejected,
        }
        if dry_run:
            return {**plan, "status": "planned"}

        ok, why = self._launch_allowed(proj)
        if not ok:
            logger.warning("research planner: refusing to launch for %s (%s)", proj.slug, why)
            await self._log_event(proj, "launch_blocked", why)
            return {**plan, "status": "skipped", "reason": why}

        try:
            started = await self._start_run(proj, chosen, depth, breadth)
        except Exception as exc:
            logger.exception("research planner: start_research failed for %s", proj.slug)
            await self._log_event(proj, "start_failed", str(exc)[:300])
            return {**plan, "status": "error", "reason": str(exc)[:200]}

        research_id = started["research_id"]
        run = await self._await_run(research_id, started.get("task_id"), budget)
        verification = await self.verify_citations(run)
        await self._record_verification(research_id, verification)

        published = None
        min_verified = int(_setting("research_planner_min_verified_sources", 1))
        if run.get("status") != "completed":
            await self._log_event(proj, "run_incomplete",
                                  f"{chosen.question[:120]} ({run.get('status')})")
        elif verification["verified"] >= min_verified:
            published = await self.publish(proj, chosen, run, verification)
        else:
            # Stored as memory by ResearchService, but NOT published: a note
            # whose every citation failed re-fetch looks exactly like a
            # well-sourced one in the vault, and that is worse than silence.
            logger.warning(
                "research planner: %s not published — %d/%d citations verified",
                chosen.question[:80], verification["verified"], verification["claimed"],
            )
            await self._log_event(
                proj, "citations_unverified",
                f"{chosen.question[:120]} — {verification['verified']}/{verification['claimed']} verified",
            )

        outcome = {
            **plan,
            "status": "completed" if run.get("status") == "completed" else "failed",
            "research_id": research_id,
            "total_tokens": run.get("total_tokens", 0),
            "learnings": len(run.get("learnings") or []),
            "citations": verification,
            "vault_path": published,
        }
        if published:
            await self._log_event(
                proj, "published",
                f"{chosen.question[:120]} -> {published} "
                f"({verification['verified']}/{verification['claimed']} citations verified)",
            )
        return outcome

    def _launch_allowed(self, project: Project) -> tuple[bool, str]:
        """Refuse to launch a run that would land on DS4.

        `ResearchService` resolves its adapter through `llm_manager.get_adapter`
        with no base_url, which means `llamacpp_identified_url` — ARIA's own
        /llm/v1 proxy, which auto-routes to the LARGEST RESIDENT model. That is
        DS4, pi's single coding slot, and a research prefill there evicts a
        coding agent's warm prefix. Until ResearchService accepts an endpoint
        (see the INTEGRATION SPEC), the only safe launch is one that cannot
        reach the unpinned local adapter at all.
        """
        if self._start_research_supports("endpoint"):
            return True, "endpoint_pinned"
        if not bool(_setting("research_planner_require_pinned_endpoint", True)):
            return True, "pin_check_disabled"
        try:
            from aria.core.claude_runner import ClaudeRunner

            if bool(_setting("use_claude_runner", True)) and ClaudeRunner.is_available():
                # Every completion goes to the cloud CLI, so no local slot is
                # touched. Safe for the box, but it spends Ben's cloud
                # subscription — so it needs the charter's permission, which is
                # what tiers_allowed is for. Unset means unconstrained.
                tiers = list(project.charter.tiers_allowed) if project.charter else []
                if tiers and "cloud" not in tiers:
                    return False, "cloud_tier_not_allowed"
                return True, "cloud_runner"
        except Exception:  # pragma: no cover - import guard only
            pass
        return False, "endpoint_unpinned"

    def _start_research_supports(self, name: str) -> bool:
        if self.research is None:
            return False
        try:
            return name in inspect.signature(self.research.start_research).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            return False

    async def _start_run(
        self, project: Project, candidate: Candidate, depth: int, breadth: int
    ) -> dict:
        """Call ResearchService as it is, passing the project/topic/endpoint
        arguments only when it has grown them. Anything it does not accept is
        written onto the run document immediately afterwards, so the planner's
        own dedup and budget queries work either way."""
        if self.research is None:
            raise RuntimeError("no ResearchService wired into the planner")
        kwargs: dict[str, Any] = {
            "query": candidate.question,
            "depth": depth,
            "breadth": breadth,
            "backend": self.backend,
            "model": self.model,
        }
        for name, value in (
            ("endpoint", self.endpoint),
            ("force_local", True),
            ("project_id", project.id),
            ("topic_hash", candidate.topic_hash),
        ):
            if self._start_research_supports(name):
                kwargs[name] = value
        started = await self.research.start_research(**kwargs)
        await self.db.research_runs.update_one(
            {"_id": started["research_id"]},
            {"$set": {
                "planner": {
                    "project_id": project.id,
                    "project_slug": project.slug,
                    "project_path": project.path,
                    "topic_hash": candidate.topic_hash,
                    "origin": candidate.origin,
                    "question": candidate.question,
                    "endpoint": self.endpoint,
                    "planned_at": _utcnow(),
                },
                # Duplicated at the top level because this is what the vault
                # frontmatter and any future index will key on, and a nested
                # field is a poor thing to build an index around later.
                "topic_hash": candidate.topic_hash,
                "project_id": project.id,
            }},
        )
        return started

    async def _await_run(self, research_id: str, task_id: Optional[str], budget: dict) -> dict:
        """Wait for the run, enforcing the budget from outside.

        `total_tokens` has always been summed and never checked; the wall clock
        was never bounded at all. Cancelling the task is the only enforcement
        available without editing ResearchService, and it is real: the task
        runner marks the task cancelled and the run stops fetching.
        """
        # `or 20` would swallow an explicit 0 ("stop immediately"), which is
        # exactly the value a test or an operator uses to prove the watchdog
        # fires — and a budget knob that silently ignores its own zero is how a
        # cap ends up believed but unenforced.
        wall = budget.get("max_wall_minutes")
        deadline = _utcnow() + timedelta(minutes=20 if wall is None else int(wall))
        # 0 here means "no cap" for tokens and sources, which is why these two
        # keep the falsy form.
        max_tokens = int(budget.get("max_tokens") or 0)
        max_sources = int(budget.get("max_sources") or 0)
        last: dict = {}
        while True:
            run = await self.db.research_runs.find_one({"_id": research_id})
            if run is None:
                return {**last, "_id": research_id, "status": "missing"}
            last = run
            status = run.get("status")
            if status in {"completed", "failed", "cancelled"}:
                return run

            breach = None
            if max_tokens and int(run.get("total_tokens") or 0) > max_tokens:
                breach = f"tokens>{max_tokens}"
            elif max_sources and len(run.get("sources") or []) > max_sources:
                breach = f"sources>{max_sources}"
            elif _utcnow() >= deadline:
                breach = f"wall>{budget.get('max_wall_minutes')}m"

            if breach:
                logger.warning("research planner: cancelling run %s (%s)", research_id, breach)
                await self._cancel_run(research_id, run.get("task_id") or task_id, breach)
                return {**run, "status": "cancelled", "budget_breach": breach}

            await asyncio.sleep(self.poll_seconds)

    async def _cancel_run(self, research_id: str, task_id: Optional[str], reason: str) -> None:
        runner = getattr(self.research, "task_runner", None)
        if runner is not None and task_id:
            try:
                await runner.cancel_task(task_id)
            except Exception:
                logger.warning("research planner: cancel_task failed for %s", task_id, exc_info=True)
        await self.db.research_runs.update_one(
            {"_id": research_id},
            {"$set": {"status": "cancelled", "budget_breach": reason, "updated_at": _utcnow()}},
        )

    # ----------------------------------------------------- 4. citation check

    async def verify_citations(self, run: dict) -> dict:
        """Re-fetch every cited URL and check the claim is actually there.

        This is THE local-model-specific control. A cloud model that invents a
        URL is embarrassing; a local model doing it into an auto-published vault
        note is a fabricated citation with ARIA's name on it. Verification is
        per-claim, not per-URL: a page that exists does not make every sentence
        attributed to it true.
        """
        learnings = [l for l in (run.get("learnings") or []) if isinstance(l, dict)]
        claims = [(str(l.get("content") or ""), l.get("source_url")) for l in learnings]
        claims = [(c, u) for c, u in claims if c and u]
        max_fetch = int(_setting("research_planner_max_citation_fetches", 12))
        urls: list[str] = []
        for _, url in claims:
            if url not in urls:
                urls.append(url)
        urls = urls[:max_fetch]

        pages: dict[str, Optional[str]] = {}
        for url in urls:
            pages[url] = await self._fetch_text(url)

        verified: list[dict] = []
        unverified: list[dict] = []
        verified_urls: list[str] = []
        for content, url in claims:
            page = pages.get(url)
            if page is None:
                # Either it was never fetched (over the fetch cap) or it does
                # not resolve. A URL a local model invented lands here, which is
                # the whole point of re-fetching rather than trusting the run.
                unverified.append({"url": url, "claim": content[:200], "reason": "unfetchable"})
                continue
            ok, reason = claim_supported(content, page)
            (verified if ok else unverified).append(
                {"url": url, "claim": content[:200], "reason": reason}
            )
            if ok and url not in verified_urls:
                verified_urls.append(url)
        return {
            "claimed": len(claims),
            "verified": len(verified),
            "urls_checked": len(urls),
            "urls_dead": [u for u, p in pages.items() if p is None],
            "verified_urls": verified_urls,
            "unverified": unverified[:20],
        }

    async def _fetch_text(self, url: str) -> Optional[str]:
        try:
            result = await self.web.execute({"url": url, "timeout": 20})
        except Exception:
            logger.debug("research planner: fetch raised for %s", url, exc_info=True)
            return None
        status = getattr(result, "status", None)
        ok = getattr(status, "value", status) == "success"
        if not ok:
            return None
        content = str((getattr(result, "output", None) or {}).get("content", ""))
        text = strip_html(content)
        return text or None

    async def _record_verification(self, research_id: str, verification: dict) -> None:
        await self.db.research_runs.update_one(
            {"_id": research_id},
            {"$set": {
                "citations": verification,
                "sources_verified": f"{verification['verified']}/{verification['claimed']}",
                "updated_at": _utcnow(),
            }},
        )

    # ------------------------------------------------------------ 6. publish

    @property
    def writer(self):
        """Lazily built so a planner constructed in a test or a scheduler tick
        does not touch the vault path until it actually publishes."""
        if self._writer is None:
            from aria.integrations.obsidian import ObsidianWriter

            self._writer = ObsidianWriter(db=self.db)
        return self._writer

    async def publish(
        self, project: Project, candidate: Candidate, run: dict, verification: dict
    ) -> Optional[str]:
        body = self._compose_note(project, candidate, run, verification)
        frontmatter = {
            "accepted": "pending",          # Ben flips this on his phone; VaultReader reads it back
            "topic_hash": candidate.topic_hash,
            "project": project.slug,
            "origin": candidate.origin,
            "research_id": str(run.get("_id") or ""),
            "sources_verified": f"{verification['verified']}/{verification['claimed']}",
            "model": run.get("model") or self.model,
            "tokens": int(run.get("total_tokens") or 0),
        }
        target = project.path or project.name
        try:
            path = await self.writer.publish(
                body,
                title=candidate.question[:120],
                doc_type="Research",
                project=target,
                frontmatter=frontmatter,
            )
        except Exception:
            logger.warning("research planner: vault publish failed", exc_info=True)
            return None
        if path:
            await self.db.research_runs.update_one(
                {"_id": run.get("_id")},
                {"$set": {"vault_path": path, "accepted": "pending", "updated_at": _utcnow()}},
            )
            await self._append_plan_line(project, candidate, path, verification)
        return path

    def _compose_note(
        self, project: Project, candidate: Candidate, run: dict, verification: dict
    ) -> str:
        report = (run.get("report_text") or "").strip()
        lines = [report or "_No synthesis was produced._", "", "## Sources", ""]
        dead = set(verification.get("urls_dead") or [])
        verified_urls = set(verification.get("verified_urls") or [])
        for source in (run.get("sources") or [])[:20]:
            url = source.get("url") or ""
            title = source.get("title") or url
            if url in verified_urls:
                mark = "verified"
            elif url in dead:
                mark = "UNREACHABLE on re-fetch"
            else:
                mark = "UNVERIFIED — no cited claim found on the page"
            lines.append(f"- [{title}]({url}) — {mark}")
        lines += [
            "",
            "## Verification",
            "",
            f"- Claims with a cited URL: {verification['claimed']}",
            f"- Claims found on the cited page: {verification['verified']}",
            f"- URLs re-fetched: {verification['urls_checked']}"
            + (f", unreachable: {len(dead)}" if dead else ""),
            "",
            "*Every citation above was re-fetched after the run and checked for the "
            "claim it supports. Unmarked sources failed that check — treat them as "
            "leads, not evidence.*",
        ]
        return "\n".join(lines)

    async def _append_plan_line(
        self, project: Project, candidate: Candidate, path: str, verification: dict
    ) -> None:
        """One line into STEWARD_PLAN.md so the project's own plan shows what
        research happened. Best-effort: the writer refuses when Ben has edited
        the file since ARIA last wrote it, and that refusal is correct."""
        try:
            await self.writer.append_section(
                "STEWARD_PLAN.md",
                "Research",
                f"- [{candidate.question[:120]}]({path}) — "
                f"{verification['verified']}/{verification['claimed']} citations verified "
                f"(`{candidate.topic_hash}`, accepted: pending)",
                project=project.path or project.name,
                doc_type="Planning",
            )
        except Exception:
            logger.debug("research planner: STEWARD_PLAN append failed", exc_info=True)

    # -------------------------------------------------------------- plumbing

    async def _resolve_project(self, project: Project | str) -> Optional[Project]:
        if isinstance(project, Project):
            return project
        return await self.planning.get_project_by_ident(str(project))

    async def _complete(self, prompt: str, *, max_tokens: int, temperature: float = 0.4) -> str:
        """One local completion, pinned to the steward endpoint.

        `base_url` is passed explicitly: without it `llm_manager` hands back the
        /llm/v1 proxy adapter, which auto-routes to the largest resident model
        (DS4 — pi's slot). An empty answer raises, because Qwen3.8's reasoning
        tokens are emitted before content and a truncated reply is an empty
        string, not a short one.
        """
        adapter = llm_manager.get_adapter(self.backend, self.model, self.endpoint)
        content, _tool_calls, _usage = await adapter.complete(
            messages=[Message(role="user", content=prompt)],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = (content or "").strip()
        if not text:
            raise EmptyCompletion(
                f"{self.model} returned empty content at max_tokens={max_tokens} "
                "(reasoning model: raise steward_max_tokens)"
            )
        return text

    async def _log_event(self, project: Project, event_type: str, detail: str) -> None:
        """Cockpit/digest material — never a page.

        `needs_human=False` and an explicit severity are mandatory, not
        defensive: `classify()` sends an unrecognised source to
        severity=medium + needs_human=True, so a planner alert that forgot them
        would Signal Ben on every completed research note.
        """
        if self.notifier is None:
            return
        severity = "low" if event_type in {"citations_unverified", "launch_blocked"} else "info"
        try:
            await self.notifier.notify(
                source=SOURCE,
                event_type=event_type,
                detail=f"{project.slug}: {detail}"[:1000],
                cooldown_seconds=0,
                severity=severity,
                kind="research",
                needs_human=False,
                dedup_key=f"{SOURCE}|{event_type}|{project.slug}",
                project_slug=project.slug,
            )
        except Exception:
            logger.debug("research planner: alert enqueue failed", exc_info=True)


# --------------------------------------------------------------------------
# Free functions used by the planner and by tests
# --------------------------------------------------------------------------

def claim_supported(claim: str, page_text: str) -> tuple[bool, str]:
    """Does `page_text` actually support `claim`?

    Three rules, strictest first:
      1. A quoted span in the claim must appear verbatim (normalised). A quote
         is a promise about exact words; a near-miss is a fabrication.
      2. Every number in the claim must appear on the page. Invented figures are
         the most citable and least checkable thing a model produces.
      3. At least `research_planner_claim_overlap` of the claim's distinctive
         tokens must appear. Below three such tokens the claim is too generic to
         verify, and unverifiable counts as unverified.
    """
    page = " " + normalize_question(page_text) + " "
    raw_page = " " + _WS.sub(" ", (page_text or "").lower()) + " "

    quotes = [q.strip() for q in _QUOTED.findall(claim or "")]
    for quote in quotes:
        needle = normalize_question(quote)
        if needle and needle not in page:
            return False, "quote_not_found"
    if quotes:
        return True, "quote_verified"

    numbers = [n.replace(",", "") for n in _NUMBER.findall(claim or "")]
    for number in numbers:
        if number and number not in raw_page.replace(",", ""):
            return False, f"number_not_found:{number}"

    tokens = [t for t in _distinctive_tokens(claim) if len(t) >= 4]
    unique = list(dict.fromkeys(tokens))
    if len(unique) < 3:
        return False, "claim_too_generic"
    hits = sum(1 for t in unique if f" {t} " in page)
    ratio = hits / len(unique)
    threshold = float(_setting("research_planner_claim_overlap", 0.6))
    if ratio >= threshold:
        return True, f"overlap:{ratio:.2f}"
    return False, f"overlap:{ratio:.2f}"


def _parse_json_array(value: str) -> list:
    """Parse the model's answer into a list, tolerating a code fence, a
    reasoning preamble, or an object wrapper. Returns [] rather than raising —
    the caller treats an empty list as a failure and falls back to seeds."""
    cleaned = (value or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # A reasoning model often prefixes prose. Take the outermost [...] span.
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start == -1 or end <= start:
            return []
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("questions", "items", "results"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
    return []


def _dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for cand in candidates:
        if not cand.question.strip():
            continue
        thash = cand.topic_hash
        if thash in seen:
            continue
        seen.add(thash)
        out.append(cand)
    return out


def _bullets(items: list[str]) -> str:
    lines = [f"- {str(i).strip()}" for i in (items or []) if str(i).strip()]
    return "\n".join(lines) if lines else "- (none)"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value) -> Optional[datetime]:
    """Mongo hands back naive datetimes; comparing one to an aware `now` raises
    TypeError, which inside the dedup loop would look like "no prior runs"."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
