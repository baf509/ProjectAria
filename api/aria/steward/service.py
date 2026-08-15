"""
ARIA - Steward Worker

Purpose: the per-project loop that turns a charter into the next action. Every
`steward_interval_minutes` it reads each chartered project's state (charter,
cockpit aggregate, open tasks, recent session outcomes, machine_scan memories,
the current STEWARD_PLAN.md), asks the local model where the gap between the
charter's goals and the observed state is, chooses at most
`steward_max_actions_per_tick` actions that its autonomy level and budget allow,
and writes back what it saw and what it chose — to `steward_runs`, to the
project's activity log, and to `STEWARD_PLAN.md` in the vault.

The four rules this module is built around, each from a documented failure:

1. **Propose, don't act.** A0 writes a plan line and nothing else; A1 proposes
   tasks and research topics; A2 may spawn a coding session but ONLY inside a
   guard worktree, and the merge stays Ben's; A3 is A2 here, because the guard
   decides merging, not the steward. A local model never gets past A2
   (`LOCAL_AUTONOMY_CAP`), per decision D2.
2. **The model may return nothing, and nothing is not zero.** Qwen3.8 is a
   reasoning model: it emits `reasoning_content` first, so a tight token budget
   comes back `finish_reason="length"` with an EMPTY `content`. Writing that
   empty result as an answer is exactly how DS4 silently labelled every memory
   with zero entities. Here an empty completion is an ERROR: the tick records
   the failure, takes no action, and says so in the plan.
3. **Ben's edit always wins.** `handle_vault_events` applies what he typed on
   his phone — a charter, an autonomy level, an approval flip, an `accepted:` on
   a research note — through the human-owned write paths (`set_charter(...,
   actor="human", via="vault")`). The steward never writes a human-owned field
   itself; a pause is *proposed*, never applied.
4. **Zero chartered projects is the normal state.** There is no charter on this
   box yet. A tick with an empty active set must cost nothing: no LLM call, no
   vault write, no alert — one `steward_runs`-free log line saying so.

Related Spec Sections:
- ARIA_PROJECT_STEWARD_PROPOSAL_20260815.md §3.1 #9, §4 (charter/autonomy/
  lifecycle), §5 step 5 (accepted), §6.3 (when to raise), §9 (which model)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from aria.config import settings
from aria.integrations.obsidian import (
    NOTES_HEADING,
    FrontmatterError,
    ObsidianWriter,
    extract_section,
    parse_frontmatter,
)
from aria.planning.models import Charter, Project, TaskCreateRequest, TaskSource
from aria.planning.service import PlanningService, effective_budget

logger = logging.getLogger(__name__)

# Worker-owned collections. `steward_runs` is the audit trail (what it saw, what
# it chose, why) and doubles as the budget ledger: the sessions and research runs
# the steward itself spent are the ones its budget bounds, so its own record is
# the authority on them.
RUNS_COLLECTION = "steward_runs"
PLANS_COLLECTION = "steward_plans"

PLAN_FILENAME = "STEWARD_PLAN.md"
PLAN_DOC_TYPE = "Planning"

ACTION_KINDS = ("task", "research", "session", "note")

AUTONOMY_NAMES = {
    0: "A0 observe",
    1: "A1 propose",
    2: "A2 execute-in-sandboxed-worktree",
    3: "A3 auto-merge",
}

# A local model caps at A2 until the eval gate in §8 is passed (D2: not before
# 20 clean A2 merges AND ≥98% tool-call reliability over ≥200 calls). A3 differs
# from A2 only in who merges, and the guard — not this worker — merges.
LOCAL_AUTONOMY_CAP = 2

# tier -> (backend, subagent_profile). `red` has no launch profile on this box
# yet, so a charter naming it gets a recorded skip rather than a silent
# substitution onto some other machine's GPU.
TIER_BACKENDS = {
    "local": ("pi-code", "pi-coding"),
    "ridge": ("pi-code", "pi-coding-ridge"),
    "cloud": ("claude_code", None),
}
CLOUD_TIERS = {"cloud"}

# Prompt budget. Qwen3.8 serves Hermes on slot 1 of the same server; a 13K cold
# prefill on slot 2 drags slot 1's decode to ~6 t/s (HOUSE_AGENT §4.1), so the
# steward's context is capped rather than "whatever the joins produced".
MAX_PROMPT_CHARS = 12000
LLM_TIMEOUT_SECONDS = 240

# How far back the budget ledger reads. 48 ticks/day/project at the default
# cadence, so 200 runs covers ~4 days — more than the longest window
# (research_runs_per_week) needs.
RUN_LEDGER_LIMIT = 200

SEVERITY_INFO = "info"

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class StewardModelError(RuntimeError):
    """The local model did not produce a usable answer.

    Raised for an empty completion as loudly as for a connection error, because
    an empty completion from a reasoning model *is* a failure — see rule 2 in
    the module docstring.
    """


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clip(text: Any, width: int = 240) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s if len(s) <= width else s[: width - 1] + "…"


def extract_json_object(text: str) -> dict:
    """Pull the first JSON object out of a reasoning model's reply.

    Qwen3.8 routinely wraps its answer in prose or a ``` fence even when told
    not to, so a bare `json.loads` fails on output that is perfectly usable.
    Brace-matching (rather than a regex) is what survives nested objects.
    """
    if not text or not text.strip():
        raise StewardModelError("model returned empty content")
    cleaned = _JSON_FENCE.sub("", text).strip()
    start = cleaned.find("{")
    if start < 0:
        raise StewardModelError(f"no JSON object in model reply: {_clip(cleaned)}")
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = cleaned[start : i + 1]
                try:
                    parsed = json.loads(blob)
                except json.JSONDecodeError as exc:
                    raise StewardModelError(f"unparseable JSON: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise StewardModelError("model reply was not a JSON object")
                return parsed
    raise StewardModelError("unterminated JSON object in model reply")


SYSTEM_PROMPT = """You are ARIA's project steward. You do not talk to the user.

You are given one project's CHARTER (why it exists, what "done" looks like) and
the OBSERVED STATE of that project right now. Your job is to name the single
most important gap between them, and to choose at most {max_actions} concrete
next actions from the allowed kinds below. Fewer is better; zero is a valid and
often correct answer.

Allowed action kinds this tick: {allowed}
  task     - a to-do to PROPOSE to the human (title = one imperative sentence)
  research - a question to investigate (title = the question)
  session  - work for a sandboxed coding agent (title = the goal, prompt = the
             full instruction to the agent; it runs in an isolated git worktree
             and CANNOT merge its own work)
  note     - an observation for the plan document; changes nothing

Rules:
- Only propose work that serves the charter's goals or success criteria.
- Never propose anything the charter's non-goals exclude.
- Do not repeat something already open in the task list or already running.
- Do not propose merging, pushing, deploying, or anything outside the repo.
- Ground every action in something in the OBSERVED STATE; if the state gives you
  nothing to act on, return an empty actions list and say why in "gap".

Answer with ONE JSON object and nothing else:
{{"assessment": "<2 sentences on where this project stands>",
  "gap": "<the single most important gap, or why there is none>",
  "actions": [{{"kind": "task|research|session|note",
                "title": "<one line>",
                "why": "<which goal or criterion this serves>",
                "prompt": "<only for kind=session: the full agent instruction>"}}]}}
"""


class StewardWorker:
    """The per-project steward loop.

    Shaped like `shells/selfcheck.py` (start/stop/evaluate-once) so it is the
    same thing to operate as every other worker here, and so the whole tick is
    callable synchronously from a route and from a test without a timer.
    """

    def __init__(
        self,
        db,
        *,
        planning: Optional[PlanningService] = None,
        notifier=None,
        writer: Optional[ObsidianWriter] = None,
        llm_manager=None,
        coding_manager=None,
        guard=None,
        shell_service=None,
        interval_minutes: Optional[int] = None,
    ):
        self.db = db
        self.planning = planning or PlanningService(db)
        self.notifier = notifier
        self.writer = writer or ObsidianWriter(db=db)
        self._llm_manager = llm_manager
        self._coding_manager = coding_manager
        self._guard = guard
        self._shell_service = shell_service
        self.interval = max(60, int(interval_minutes or settings.steward_interval_minutes) * 60)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._indexes_ready = False
        self.last_tick: Optional[dict] = None
        self.ticks = 0

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        if not settings.steward_enabled:
            # Self-guard as well as the caller's guard: this loop can spawn
            # coding agents, and "off by default" must not depend on one `if`
            # in main.py staying correct through every future edit.
            logger.info("steward worker not started (steward_enabled=false)")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="steward.worker")
        logger.info(
            "steward worker started (every %ds, %s, max %d action(s)/tick)",
            self.interval, settings.steward_model, settings.steward_max_actions_per_tick,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=120)  # settle on boot
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # pragma: no cover - a tick must never kill the loop
                logger.warning("steward tick failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def _ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        self._indexes_ready = True
        try:
            await self.db[RUNS_COLLECTION].create_index([("slug", 1), ("started_at", -1)])
            await self.db[RUNS_COLLECTION].create_index([("started_at", -1)])
        except Exception as exc:  # pragma: no cover - non-fatal
            logger.debug("steward: index creation skipped: %s", exc)

    # ------------------------------------------------------------------ tick

    async def tick(self) -> dict:
        """One pass over the active set. Safe — and cheap — with zero charters."""
        started = _now()
        self.ticks += 1
        await self._ensure_indexes()
        try:
            projects = await self.planning.active_projects()
        except Exception as exc:
            logger.warning("steward: could not read the active set: %s", exc)
            summary = {"at": started, "projects": 0, "error": str(exc), "runs": []}
            self.last_tick = summary
            return summary

        if not projects:
            # The state of this box today: no charter exists. Nothing to do, and
            # doing nothing must cost nothing — no model call, no vault write,
            # no `steward_runs` row per empty tick.
            logger.info(
                "steward: no chartered projects (status=active AND kind=project AND "
                "charter.purpose) — nothing to steward"
            )
            summary = {
                "at": started, "projects": 0, "runs": [],
                "reason": "no chartered projects",
            }
            self.last_tick = summary
            return summary

        shared = await self._shared_context(projects)
        runs = []
        for project in projects:
            try:
                runs.append(await self.tick_project(project, shared=shared))
            except Exception as exc:
                logger.warning("steward: tick failed for %s: %s", project.slug, exc, exc_info=True)
                runs.append({"slug": project.slug, "error": str(exc)})
        summary = {
            "at": started,
            "projects": len(projects),
            "runs": [
                {
                    "slug": r.get("slug"),
                    "status": r.get("status"),
                    "actions": len(r.get("actions_executed") or []),
                    "error": r.get("error"),
                }
                for r in runs
            ],
            "duration_ms": int((_now() - started).total_seconds() * 1000),
        }
        self.last_tick = summary
        return summary

    async def tick_project(
        self, project: Project, *, shared: Optional[dict] = None,
        dry_run: bool = False, trigger: str = "worker",
    ) -> dict:
        """Observe → find the gap → choose → execute → write back, for one project."""
        started = _now()
        run: dict = {
            "run_id": uuid4().hex,
            "slug": project.slug,
            "project_id": project.id,
            "project_name": project.name,
            "trigger": trigger,
            "dry_run": dry_run,
            "started_at": started,
            "status": "observed",
            "actions_proposed": [],
            "actions_executed": [],
            "skipped": [],
        }
        charter = project.charter or Charter()
        steward_state = project.steward

        # A project the steward has stood down on stays stood down until a human
        # clears the reason (it is set only by propose_pause, which is a question
        # to Ben). Re-planning it every 30 minutes would be nagging in a loop.
        paused_reason = getattr(steward_state, "paused_reason", None)
        if paused_reason:
            run.update(
                status="standing-down",
                reason=f"steward.paused_reason: {paused_reason}",
                finished_at=_now(),
            )
            if not dry_run:
                await self._record_run(run)
            return run

        shared = shared or await self._shared_context([project])
        observed = await self._observe(project, shared)
        budget = await self._budget_state(project, charter, observed)
        run["observed"] = observed
        run["budget"] = budget

        approval = observed["plan"].get("approval")
        autonomy = int(charter.autonomy or 0)
        tier = self._pick_tier(charter, budget)
        autonomy_effective = self._effective_autonomy(autonomy, tier, approval)
        run.update(
            autonomy=autonomy,
            autonomy_effective=autonomy_effective,
            autonomy_label=AUTONOMY_NAMES.get(autonomy_effective, str(autonomy_effective)),
            tier=tier,
            approval=approval,
        )

        allowed = self._allowed_kinds(autonomy_effective, budget, tier)
        run["allowed_kinds"] = sorted(allowed)

        # The pause proposal is a charter-independent lifecycle question, so it
        # is evaluated before (and regardless of) whatever the model says.
        pause = await self._maybe_propose_pause(project, observed, dry_run=dry_run)
        if pause:
            run["pause_proposal"] = pause

        decision: dict = {"assessment": "", "gap": "", "actions": []}
        model_info: dict = {
            "backend": settings.steward_backend,
            "model": settings.steward_model,
            "endpoint": settings.steward_endpoint,
            "ok": False,
        }
        if autonomy_effective <= 0 and not allowed - {"note"}:
            # A0: a plan line and a digest entry, nothing else — and no model
            # call, because there is no decision for it to make.
            model_info["skipped"] = "A0 observe — no action to choose"
            decision["gap"] = "Autonomy A0: observing only."
        else:
            try:
                decision, usage = await self._ask_model(project, charter, observed, allowed)
                model_info.update(ok=True, usage=usage)
            except StewardModelError as exc:
                # Rule 2: never write the empty result. No actions this tick.
                model_info.update(ok=False, error=str(exc))
                logger.warning("steward: model failed for %s: %s", project.slug, exc)
            except Exception as exc:
                model_info.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                logger.warning("steward: model call failed for %s: %s", project.slug, exc)
        run["model"] = model_info
        run["assessment"] = _clip(decision.get("assessment"), 600)
        run["gap"] = _clip(decision.get("gap"), 600)

        proposed = self._sanitize_actions(decision.get("actions"), allowed, run)
        run["actions_proposed"] = proposed
        if not dry_run:
            for action in proposed:
                result = await self._execute(project, charter, action, tier, run)
                if result.get("executed"):
                    run["actions_executed"].append(result)
                else:
                    run["skipped"].append(result)
        run["status"] = self._run_status(run, autonomy_effective, budget)

        if not dry_run:
            plan = await self._write_plan(project, charter, run, observed, budget)
            run["plan_write"] = plan
            await self._persist_state(project, run, observed, plan)
            await self._record_run(run)
        run["finished_at"] = _now()
        run["duration_ms"] = int((run["finished_at"] - started).total_seconds() * 1000)
        return run

    # ------------------------------------------------------------- observing

    async def _shared_context(self, projects: list[Project]) -> dict:
        """The cockpit's raw material, gathered once per tick for every project.

        Imported from `api/routes/digest.py` rather than reimplemented: the
        path→project attribution rule (most-specific-root wins, `PathIndex`) and
        the attention score are policy, and a second copy of them would drift
        from the cockpit Ben is actually looking at. The import is deliberately
        lazy — the routes module pulls in `api.deps`, and importing that at
        module scope from a service makes an import cycle out of a read model.
        """
        from aria.api.routes import digest as cockpit

        try:
            all_projects = await self.planning.list_projects()
        except Exception:
            all_projects = list(projects)
        known = {p.id for p in all_projects}
        all_projects.extend(p for p in projects if p.id not in known)

        shell_service = await self._resolve_shell_service()
        ctx = await cockpit._gather_context(self.db, shell_service)
        return {"ctx": ctx, "index": cockpit.PathIndex(all_projects), "cockpit": cockpit}

    async def _observe(self, project: Project, shared: dict) -> dict:
        cockpit = shared["cockpit"]
        ctx, index = shared["ctx"], shared["index"]
        now = ctx["now"]

        open_tasks = await self.planning.list_tasks(
            status=["proposed", "active"], project_id=project.id, limit=200
        )
        attention = cockpit._project_attention(project, ctx, open_tasks, now, index)
        sessions = [s for s in ctx["sessions"] if index.session_owner(s) == project.id]
        alerts = [a for a in ctx["alerts"] if index.owner(a.get("project_path")) == project.id]
        roots = cockpit.project_roots(project)

        changed = []
        if roots:
            try:
                docs = await self.db.memories.find(
                    {"source.type": "machine_scan", "source.repo": {"$in": roots}}
                ).sort("created_at", -1).to_list(length=8)
                changed = [_clip(d.get("content"), 200) for d in docs]
            except Exception as exc:
                logger.debug("steward: machine_scan read failed for %s: %s", project.slug, exc)

        outcomes = []
        try:
            docs = await self.db.session_outcomes.find(
                {"project_slug": project.slug}
            ).sort("created_at", -1).to_list(length=5)
            outcomes = [
                {"label": d.get("label"), "summary": _clip(d.get("summary"), 160),
                 "at": _as_utc(d.get("created_at"))}
                for d in docs
            ]
        except Exception as exc:
            # OutcomeScorer (§3.1 #13) has not landed yet; its absence must read
            # as "no labels", never as a tick failure.
            logger.debug("steward: session_outcomes unavailable: %s", exc)

        last_activity = _as_utc(project.last_activity_at) or _as_utc(project.last_signal_at)
        idle_days = (now - last_activity).total_seconds() / 86400 if last_activity else None

        return {
            "at": now,
            "attention": attention,
            "attention_score": cockpit.attention_score(attention),
            "open_tasks": [
                {"title": t.title, "status": t.status, "updated_at": _as_utc(t.updated_at)}
                for t in open_tasks[:12]
            ],
            "open_task_count": len(open_tasks),
            "sessions": [
                {
                    "id": str(s.get("_id")),
                    "status": s.get("status"),
                    "backend": s.get("backend"),
                    "summary": _clip(s.get("result_summary"), 160),
                    "gate_failed": cockpit._last_gate_failed(s),
                }
                for s in sessions[:6]
            ],
            "alerts": [
                {"source": a.get("source"), "event_type": a.get("event_type"),
                 "message": _clip(a.get("message"), 160)}
                for a in alerts[:6]
            ],
            "changed": changed,
            "outcomes": outcomes,
            "git": project.git or {},
            "next_steps": list(project.next_steps or [])[:5],
            "check_command": project.check_command,
            "idle_days": round(idle_days, 1) if idle_days is not None else None,
            "plan": await self._read_plan(project),
        }

    async def _resolve_shell_service(self):
        """The real ShellService when this process has one, else nothing.

        `_gather_context` degrades to an empty shell list on any failure, so a
        missing fleet costs the blocked-shell signal and nothing else — which is
        the right trade for a worker that must run on a box where the shells
        substrate is switched off.
        """
        if self._shell_service is not None:
            return self._shell_service
        try:
            from aria.api.deps import get_shell_service

            self._shell_service = await get_shell_service(self.db)
        except Exception as exc:
            logger.debug("steward: no shell service (%s)", exc)
            self._shell_service = _NoShells()
        return self._shell_service

    def plan_path(self, project: Project) -> Path:
        """Where this project's STEWARD_PLAN.md lives.

        Resolved through the writer's own folder rule where possible: reading a
        different file than the one we write is the failure that would make the
        approval gate read `pending` forever.
        """
        folder_for = getattr(self.writer, "_folder_for", None)
        if callable(folder_for):
            try:
                return Path(folder_for(project.path, PLAN_DOC_TYPE)) / PLAN_FILENAME
            except Exception as exc:  # pragma: no cover - convention fallback below
                logger.debug("steward: folder resolution fell back (%s)", exc)
        name = os.path.basename((project.path or "").rstrip("/"))
        if not name or name.startswith("."):
            name = settings.obsidian_default_folder
        return Path(settings.obsidian_vault_path) / name / PLAN_DOC_TYPE / PLAN_FILENAME

    async def _read_plan(self, project: Project) -> dict:
        """Read the plan doc from disk. The FILE is the authority on `approval:`.

        Not the Mongo mirror: a vault event can be missed (reader disabled, a
        restart mid-poll), and gating execution on a stale copy would either
        execute what Ben rejected or stall on what he approved.
        """
        path = self.plan_path(project)
        state = {}
        try:
            state = await self.db[PLANS_COLLECTION].find_one({"_id": project.slug}) or {}
        except Exception as exc:
            logger.debug("steward: plan state read failed for %s: %s", project.slug, exc)

        info: dict = {
            "path": str(path),
            "exists": False,
            # Deliberately NOT seeded from the Mongo mirror: an approval ARIA
            # merely REMEMBERS is not an approval. If the doc is gone, unreadable
            # or the vault is unmounted, execution fails closed to A1 — the
            # mirror below is a record of when he answered, not the answer.
            "approval": None,
            "recorded_approval": state.get("approval"),
            "notes": state.get("notes_from_ben"),
            "has_notes_section": False,
            "excerpt": "",
            # What ARIA last COMPOSED for this doc, so an unchanged plan is not
            # rewritten (see _write_plan). Separate from the writer's own hash,
            # which covers the frontmatter and therefore changes every tick.
            "body_hash": state.get("body_hash"),
            "plan_hash": state.get("plan_hash"),
        }
        try:
            text = await asyncio.to_thread(
                lambda: path.read_text(encoding="utf-8") if path.exists() else None
            )
        except Exception as exc:
            logger.debug("steward: plan read failed for %s: %s", path, exc)
            text = None
        if text is None:
            return info
        info["exists"] = True
        try:
            frontmatter, body = parse_frontmatter(text)
        except FrontmatterError as exc:
            # The doc is Ben's to fix; the steward reports it and treats the
            # approval as absent (fail closed — an unreadable approval is not an
            # approval).
            info["parse_error"] = str(exc)
            return info
        approval = frontmatter.get("approval")
        if isinstance(approval, str):
            info["approval"] = approval.strip().lower()
        notes = extract_section(body, NOTES_HEADING)
        info["has_notes_section"] = NOTES_HEADING.lower() in body.lower()
        if notes:
            info["notes"] = notes
        info["excerpt"] = _clip(body, 800)
        return info

    # --------------------------------------------------------------- budget

    async def _recent_runs(self, slug: str, limit: int = RUN_LEDGER_LIMIT) -> list[dict]:
        try:
            return await self.db[RUNS_COLLECTION].find({"slug": slug}).sort(
                "started_at", -1
            ).limit(limit).to_list(length=limit)
        except Exception as exc:
            logger.debug("steward: run ledger read failed for %s: %s", slug, exc)
            return []

    async def _budget_state(self, project: Project, charter: Charter, observed: dict) -> dict:
        """What is left of this project's budget today/this week.

        The ledger is the steward's OWN runs, not every session in the repo: the
        budget bounds unattended work, so a day of Ben's own coding must not
        silently disable the steward, and the steward must not get extra
        headroom on a quiet day either.
        """
        limits = effective_budget(charter)
        runs = await self._recent_runs(project.slug)
        day_cutoff = _now() - timedelta(days=1)
        week_cutoff = _now() - timedelta(days=7)

        sessions_today, research_week, conversations = 0, 0, []
        for run in runs:
            started = _as_utc(run.get("started_at"))
            if not started:
                continue
            for action in run.get("actions_executed") or []:
                kind = action.get("kind")
                if kind == "session" and started >= day_cutoff:
                    sessions_today += 1
                    if action.get("conversation_id"):
                        conversations.append(action["conversation_id"])
                elif kind == "research" and started >= week_cutoff:
                    research_week += 1

        cloud_spend = await self._cloud_spend(conversations)
        cloud_cap = limits.get("cloud_usd_per_day")
        return {
            "limits": limits,
            "sessions_today": sessions_today,
            "sessions_remaining": max(0, int(limits["sessions_per_day"]) - sessions_today),
            "research_this_week": research_week,
            "research_remaining": max(
                0, int(limits["research_runs_per_week"]) - research_week
            ),
            "cloud_usd_today": cloud_spend,
            # Usage accounting has recorded nothing since 2026-07-30, so an
            # unmeasurable spend is reported as unknown and the cap is NOT
            # enforced on a number we do not have. The session count is the
            # guard that still works.
            "cloud_measured": cloud_spend is not None,
            "cloud_exhausted": bool(
                cloud_spend is not None and cloud_cap is not None and cloud_spend >= cloud_cap
            ),
            "session_minutes": int(limits["session_minutes"]),
        }

    async def _cloud_spend(self, conversation_ids: list[str]) -> Optional[float]:
        """Priced spend for the steward's own sessions today, or None if the
        usage pipeline cannot answer."""
        if not conversation_ids:
            return 0.0
        try:
            from aria.db.usage import UsageRepo

            repo = UsageRepo(self.db)
            total = 0.0
            for conv in set(conversation_ids):
                row = await repo.cost_for_conversation(conv, days=1)
                total += float(row.get("cost") or 0.0)
            return round(total, 6)
        except Exception as exc:
            logger.debug("steward: cloud spend unmeasurable: %s", exc)
            return None

    # --------------------------------------------------------- autonomy gate

    def _pick_tier(self, charter: Charter, budget: dict) -> str:
        """The tier this project's next session would run on.

        `tiers_allowed` order is the preference order — it is the only knob Ben
        has for this, and inventing a second one in config would let the two
        disagree. Local is the default because unattended on-box concurrency is
        exactly one sandboxed session (§9) and local tokens cost nothing.
        """
        tiers = [t for t in (charter.tiers_allowed or ["local"]) if t in TIER_BACKENDS]
        if not tiers:
            tiers = ["local"]
        for tier in tiers:
            if tier in CLOUD_TIERS and budget.get("cloud_exhausted"):
                continue
            return tier
        return tiers[0]

    def _effective_autonomy(self, autonomy: int, tier: str, approval: Optional[str]) -> int:
        effective = max(0, min(3, int(autonomy or 0)))
        if tier not in CLOUD_TIERS:
            effective = min(effective, LOCAL_AUTONOMY_CAP)
        if effective >= 2 and approval != "approved":
            # The vault is the approval surface (D10): a plan Ben has not
            # approved may be proposed, never executed. `pending`, `rejected`,
            # a missing key and an unparseable doc all fail closed to A1.
            effective = 1
        return effective

    def _allowed_kinds(self, autonomy: int, budget: dict, tier: str) -> set[str]:
        allowed = {"note"}
        if autonomy >= 1:
            allowed.add("task")
            if budget["research_remaining"] > 0:
                allowed.add("research")
        if autonomy >= 2 and budget["sessions_remaining"] > 0 and tier in TIER_BACKENDS:
            allowed.add("session")
        return allowed

    def _run_status(self, run: dict, autonomy: int, budget: dict) -> str:
        if run["actions_executed"]:
            return "executing" if any(
                a.get("kind") == "session" for a in run["actions_executed"]
            ) else "proposing"
        if not (run.get("model") or {}).get("ok") and "skipped" not in (run.get("model") or {}):
            return "model-failed"
        if autonomy >= 2 and budget["sessions_remaining"] <= 0:
            return "budget-exhausted"
        return "observing"

    # -------------------------------------------------------------- the model

    def _adapter(self):
        if self._llm_manager is None:
            from aria.llm.manager import LLMManager

            self._llm_manager = LLMManager()
        return self._llm_manager.get_adapter(
            settings.steward_backend,
            settings.steward_model,
            base_url=settings.steward_endpoint,
        )

    async def _ask_model(
        self, project: Project, charter: Charter, observed: dict, allowed: set[str]
    ) -> tuple[dict, dict]:
        from aria.llm.base import Message

        system = SYSTEM_PROMPT.format(
            max_actions=settings.steward_max_actions_per_tick,
            allowed=", ".join(sorted(allowed)),
        )
        user = self._render_state(project, charter, observed)
        adapter = self._adapter()
        content, _tool_calls, usage = await asyncio.wait_for(
            adapter.complete(
                [Message(role="system", content=system), Message(role="user", content=user)],
                temperature=0.2,
                # Generous on purpose: Qwen3.8 spends tokens on
                # `reasoning_content` BEFORE `content`, so a tight budget returns
                # finish_reason="length" with content="" — measured 2026-08-15, a
                # one-line reply needed 41 completion tokens, 17 of them
                # reasoning.
                max_tokens=settings.steward_max_tokens,
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
        if not content or not content.strip():
            raise StewardModelError(
                "empty content from %s (reasoning model spent the whole budget "
                "thinking — raise steward_max_tokens)" % settings.steward_model
            )
        return extract_json_object(content), (usage or {})

    def _render_state(self, project: Project, charter: Charter, observed: dict) -> str:
        def bullets(items, prefix="- ") -> str:
            return "\n".join(f"{prefix}{i}" for i in items) if items else "- (none)"

        att = observed["attention"]
        plan = observed["plan"]
        parts = [
            f"# PROJECT {project.name} ({project.slug})",
            f"path: {project.path or '(none)'}",
            "",
            "# CHARTER",
            f"purpose: {charter.purpose}",
            "goals:", bullets(charter.goals),
            "success_criteria:", bullets(charter.success_criteria),
            "non_goals:", bullets(charter.non_goals),
            f"research_topics: {', '.join(charter.research_topics) or '(none)'}",
            "",
            "# OBSERVED STATE",
            f"attention score: {observed['attention_score']} "
            f"(blocked shells {att['blocked_shells']}, gate-failed sessions "
            f"{att['gate_failed_sessions']}, unacked alerts {att['unacked_alerts']}, "
            f"stale tasks {att['stale_tasks']})",
            f"idle days: {observed['idle_days']}",
            f"git: {json.dumps(observed['git'], default=str)[:300]}",
            "open tasks:",
            bullets([f"[{t['status']}] {t['title']}" for t in observed["open_tasks"]]),
            "recent sessions:",
            bullets(
                [f"{s['status']} ({s['backend']}) {s['summary']}" for s in observed["sessions"]]
            ),
            "recent outcomes:",
            bullets([f"{o['label']}: {o['summary']}" for o in observed["outcomes"]]),
            "unacked alerts:",
            bullets([f"{a['source']} {a['event_type']}: {a['message']}" for a in observed["alerts"]]),
            "recent repo changes:",
            bullets(observed["changed"]),
            "next steps on record:",
            bullets(observed["next_steps"]),
        ]
        if plan.get("notes"):
            # Ben's own words about this plan outrank everything above them.
            parts += ["", "# NOTES FROM BEN (authoritative)", _clip(plan["notes"], 1200)]
        text = "\n".join(parts)
        if len(text) > MAX_PROMPT_CHARS:
            text = text[:MAX_PROMPT_CHARS] + "\n… (truncated)"
        return text

    def _sanitize_actions(self, raw: Any, allowed: set[str], run: dict) -> list[dict]:
        """Trust nothing the model returned: kind, count and shape are ours."""
        actions: list[dict] = []
        if not isinstance(raw, list):
            if raw:
                run["skipped"].append({"kind": "?", "reason": "actions was not a list"})
            return actions
        for item in raw:
            if len(actions) >= settings.steward_max_actions_per_tick:
                run["skipped"].append({"kind": "?", "reason": "over max_actions_per_tick"})
                break
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            title = str(item.get("title") or "").strip()
            if kind not in ACTION_KINDS:
                run["skipped"].append({"kind": kind or "?", "reason": "unknown action kind"})
                continue
            if not title:
                run["skipped"].append({"kind": kind, "reason": "no title"})
                continue
            if kind not in allowed:
                run["skipped"].append(
                    {"kind": kind, "title": _clip(title, 120),
                     "reason": "not permitted at this autonomy/budget"}
                )
                continue
            actions.append({
                "kind": kind,
                "title": _clip(title, 300),
                "why": _clip(item.get("why"), 300),
                "prompt": str(item.get("prompt") or "").strip()[:4000],
            })
        return actions

    # ------------------------------------------------------------- execution

    async def _execute(
        self, project: Project, charter: Charter, action: dict, tier: str, run: dict
    ) -> dict:
        kind = action["kind"]
        try:
            if kind == "note":
                return {**action, "executed": True, "detail": "recorded in the plan"}
            if kind == "task":
                return await self._propose_task(project, action)
            if kind == "research":
                return await self._propose_research(project, action)
            if kind == "session":
                return await self._start_session(project, charter, action, tier, run)
        except Exception as exc:
            logger.warning(
                "steward: action %s failed for %s: %s", kind, project.slug, exc, exc_info=True
            )
            return {**action, "executed": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {**action, "executed": False, "reason": "unhandled action kind"}

    async def _propose_task(self, project: Project, action: dict) -> dict:
        """A task the steward proposes is `status=proposed` — Ben promotes it.

        Deduped against open tasks by the planning service's own normalized
        title hash, so a steward that keeps seeing the same gap files one task,
        not one per tick.
        """
        from aria.planning.service import _content_hash

        existing = await self.planning.find_open_task_by_hash(_content_hash(action["title"]))
        if existing:
            return {**action, "executed": False, "reason": "already open", "task_id": existing.id}
        task = await self.planning.create_task(
            TaskCreateRequest(
                title=action["title"],
                notes=action.get("why") or None,
                project_id=project.id,
                status="proposed",
                tags=["steward"],
            ),
            source=TaskSource(type="awareness", extracted_at=_now(), confidence=0.5),
        )
        return {**action, "executed": True, "task_id": task.id}

    async def _propose_research(self, project: Project, action: dict) -> dict:
        """A research topic is recorded, not run.

        Running it belongs to the ResearchPlanner (§3.1 #10), which owns the
        dedup/cool-down/citation-check machinery this worker deliberately does
        not duplicate. The proposal on the run doc is its queue.
        """
        return {
            **action,
            "executed": True,
            "topic_hash": _digest(action["title"].strip().lower())[:16],
            "detail": "proposed; ResearchPlanner runs it",
        }

    async def _start_session(
        self, project: Project, charter: Charter, action: dict, tier: str, run: dict
    ) -> dict:
        """A2: a coding session, always inside a guard worktree.

        The guard prepares the worktree, branch and start tag FIRST and the
        session is launched into it with `create_worktree=False` — the session
        must never touch the live checkout, and the guard (not the agent) holds
        the pen for every commit, merge and rollback afterwards.
        """
        if not project.path:
            return {**action, "executed": False, "reason": "project has no repo path"}
        backend, profile = TIER_BACKENDS[tier]

        guard = self._resolve_guard()
        # Keyed by a steward action id because the coding session id does not
        # exist until after the worktree it launches into does. The run doc
        # carries both ids so `/guard/sessions/{id}/…` stays reachable; when
        # session.py grows native guard wiring the two collapse into one id.
        guard_session_id = f"stw-{run['run_id'][:12]}-{len(run['actions_executed']) + 1}"
        prepared = await guard.prepare_session(
            project.path, guard_session_id, project_slug=project.slug
        )
        workspace = prepared.get("worktree")
        if not workspace:
            return {**action, "executed": False, "reason": "guard returned no worktree"}

        manager = await self._resolve_coding_manager()
        session = await manager.start_session(
            workspace=workspace,
            backend=backend,
            prompt=self._session_prompt(project, charter, action),
            subagent_profile=profile,
            create_worktree=False,
            loop={
                "deadline_minutes": run["budget"]["session_minutes"],
                "gate_command": project.check_command or None,
            },
        )
        session_id = str(session.get("id") or session.get("_id") or "")
        # Attribution the cockpit's attention score and every later join need.
        # Sessions carry only a workspace path today, and a worktree path is not
        # the project path, so without this a steward session is invisible to
        # the surface that is supposed to show it (§4.2).
        try:
            await self.db.coding_sessions.update_one(
                {"_id": session.get("id") or session.get("_id")},
                {"$set": {
                    "project_slug": project.slug,
                    "project_id": project.id,
                    "steward": {
                        "run_id": run["run_id"],
                        "guard_session_id": guard_session_id,
                        "tier": tier,
                    },
                }},
            )
        except Exception as exc:
            logger.debug("steward: could not stamp session attribution: %s", exc)
        return {
            **action,
            "executed": True,
            "session_id": session_id,
            "conversation_id": session.get("agent_conversation_id")
            or session.get("conversation_id"),
            "guard_session_id": guard_session_id,
            "branch": prepared.get("branch"),
            "worktree": workspace,
            "backend": backend,
            "tier": tier,
        }

    def _session_prompt(self, project: Project, charter: Charter, action: dict) -> str:
        """The agent's instruction. The constraints are re-stated in the prompt
        because a compacted context loses them (governance decay, §13) — the
        guard enforces them regardless, but an agent that knows the rules wastes
        fewer turns discovering them."""
        allowed = charter.guard.allowed_paths or ["(the whole repo)"]
        lines = [
            action["prompt"] or action["title"],
            "",
            f"Project: {project.name}. Charter purpose: {charter.purpose}",
        ]
        if action.get("why"):
            lines.append(f"Why this matters: {action['why']}")
        lines += [
            "",
            "Constraints (enforced by ARIA's guard, not by you):",
            "- You are in an isolated git worktree on your own branch. Do NOT merge, "
            "rebase onto, push to, or check out any other branch.",
            "- Do NOT commit: ARIA checkpoints your work for you.",
            f"- Stay inside: {', '.join(allowed)}",
            "- Do not edit guard, watchdog, killswitch, test fixtures or CI config.",
        ]
        if project.check_command:
            lines.append(f"- Your work is verified with: {project.check_command}")
        return "\n".join(lines)

    def _resolve_guard(self):
        if self._guard is None:
            from aria.guard.gitguard import get_git_guard

            self._guard = get_git_guard(self.db)
        return self._guard

    async def _resolve_coding_manager(self):
        """The ONE coding session manager in this process.

        Constructing a second one would give it a second concurrency counter,
        and two counters over one box means the `coding_max_concurrent_*` caps
        (sized against a 9 GiB spawn floor) stop meaning anything.
        """
        if self._coding_manager is None:
            from aria.api.deps import get_coding_session_manager

            self._coding_manager = await get_coding_session_manager(self.db)
        return self._coding_manager

    # -------------------------------------------------------- pause proposal

    async def _maybe_propose_pause(
        self, project: Project, observed: dict, *, dry_run: bool
    ) -> Optional[dict]:
        idle_days = observed.get("idle_days")
        threshold = settings.steward_idle_days_before_pause_proposal
        if idle_days is None or idle_days < threshold:
            return None
        reason = (
            f"no activity for {idle_days:.0f} days "
            f"(threshold {threshold}); nothing for the steward to advance"
        )
        if dry_run:
            return {"proposed": False, "dry_run": True, "reason": reason}
        # Never a status change: `status` is human-owned (§4.2). This files a
        # scan_review item and sets steward.paused_reason, which stops the
        # steward working the project while Ben decides.
        ok = await self.planning.propose_pause(project.slug, reason)
        await self._notify(
            event_type="pause_proposed",
            detail=f"{project.slug}: {reason}. Resume with POST /steward/projects/{project.slug}/resume",
            needs_human=True,
            severity="low",
            project_slug=project.slug,
            project_path=project.path,
            dedup_key=f"steward|pause|{project.slug}",
            cooldown_seconds=86400,
            proposal={"action": "pause_project", "slug": project.slug, "reason": reason},
        )
        return {"proposed": bool(ok), "reason": reason}

    # ------------------------------------------------------------ write-back

    async def _write_plan(
        self, project: Project, charter: Charter, run: dict, observed: dict, budget: dict
    ) -> dict:
        body = self._plan_body(project, charter, run, observed, budget)
        body_hash = _digest(body)[:16]
        if observed["plan"].get("exists") and observed["plan"].get("body_hash") == body_hash:
            # Nothing to say that the doc does not already say. Rewriting anyway
            # would bump `updated:` and push a new version through LiveSync to
            # Ben's phone every 30 minutes for no change — and on a doc he has
            # edited it would file an identical `.aria-proposed.md` every tick.
            return {
                "wrote": False, "reason": "unchanged", "path": observed["plan"]["path"],
                "hash": observed["plan"].get("plan_hash"), "body_hash": body_hash,
            }
        frontmatter = {
            "title": f"{project.name} — steward plan",
            "status": run["status"],
            "project": project.slug,
            "autonomy": int(charter.autonomy or 0),
            "approval": "pending",
            "attention_score": observed["attention_score"],
            "last_run_at": _now(),
            "plan_hash": body_hash,
            "sessions_today": budget["sessions_today"],
        }
        result = await self.writer.upsert_managed(
            PLAN_FILENAME,
            frontmatter,
            body,
            managed_keys=[
                "status", "project", "attention_score", "last_run_at",
                "plan_hash", "sessions_today",
            ],
            project=project.path,
            doc_type=PLAN_DOC_TYPE,
            title=f"{project.name} — steward plan",
        )
        if result is None:
            return {"wrote": False, "reason": "vault disabled or unwritable"}
        result["body_hash"] = body_hash
        if result.get("wrote") is False:
            logger.info(
                "steward: %s is Ben's document (%s); plan proposed at %s",
                result.get("path"), result.get("reason"), result.get("proposal_path"),
            )
        return result

    def _plan_body(
        self, project: Project, charter: Charter, run: dict, observed: dict, budget: dict
    ) -> str:
        att = observed["attention"]
        limits = budget["limits"]
        model = run.get("model") or {}
        lines = [
            f"*Autonomy {AUTONOMY_NAMES.get(run['autonomy_effective'], '?')}"
            f"{' (capped from A%d)' % run['autonomy'] if run['autonomy_effective'] < run['autonomy'] else ''}"
            f" · tier {run['tier']} · {run['status']}*",
            "",
            "## What ARIA sees",
            f"- Attention score **{observed['attention_score']}** — "
            f"{att['blocked_shells']} blocked shell(s), {att['gate_failed_sessions']} "
            f"gate-failed session(s), {att['unacked_alerts']} unacked alert(s), "
            f"{att['stale_tasks']} stale task(s)",
            f"- {observed['open_task_count']} open task(s); "
            f"idle {observed['idle_days'] if observed['idle_days'] is not None else '?'} day(s)",
        ]
        if observed["changed"]:
            lines.append(f"- Recent repo changes: {observed['changed'][0]}")
        if observed["outcomes"]:
            labels = ", ".join(o["label"] or "?" for o in observed["outcomes"])
            lines.append(f"- Recent session outcomes: {labels}")

        lines += ["", "## The gap"]
        if model.get("ok"):
            lines.append(run.get("gap") or "_(none named)_")
            if run.get("assessment"):
                lines += ["", f"_{run['assessment']}_"]
        elif model.get("skipped"):
            lines.append(f"_Not assessed — {model['skipped']}._")
        else:
            # Never write a fabricated "no gaps found" over a model failure.
            lines.append(
                f"_The steward could not assess this tick: {model.get('error', 'model unavailable')}. "
                "No actions were taken._"
            )

        lines += ["", "## Plan"]
        if run["actions_executed"]:
            for action in run["actions_executed"]:
                mark = {"task": "proposed task", "research": "research topic",
                        "session": "coding session", "note": "note"}.get(action["kind"], action["kind"])
                detail = action.get("session_id") or action.get("task_id") or action.get("detail") or ""
                lines.append(f"- **{mark}** — {action['title']}"
                             f"{f' ({detail})' if detail else ''}")
                if action.get("why"):
                    lines.append(f"  - _{action['why']}_")
        else:
            lines.append("- _Nothing this tick._")
        for skipped in run["skipped"]:
            lines.append(
                f"- ~~{skipped.get('title') or skipped.get('kind')}~~ — not done: {skipped.get('reason')}"
            )

        lines += [
            "",
            "## Budget",
            f"- Sessions today: {budget['sessions_today']}/{limits['sessions_per_day']}"
            f" ({budget['sessions_remaining']} left)",
            f"- Research runs this week: {budget['research_this_week']}/"
            f"{limits['research_runs_per_week']}",
            f"- Cloud spend today: "
            + (
                f"${budget['cloud_usd_today']:.2f} / ${limits['cloud_usd_per_day']:.2f}"
                if budget["cloud_measured"]
                else "unmeasured (usage accounting is not recording) — the session "
                     "count is the cap that applies"
            ),
        ]
        if budget["sessions_remaining"] <= 0:
            lines.append(
                "- **Budget exhausted — the steward proposes only until the window rolls over.**"
            )

        lines += ["", "## Decisions for Ben"]
        if run["autonomy"] >= 2 and run["autonomy_effective"] < 2:
            lines.append(
                "- Set `approval: approved` in this file's frontmatter to let ARIA "
                "execute this plan. Until then it proposes only."
            )
        if run.get("pause_proposal"):
            lines.append(f"- **Pause proposed**: {run['pause_proposal']['reason']}")
        if not lines[-1].startswith("- "):
            lines.append("- _None pending._")

        if not observed["plan"].get("has_notes_section"):
            # Only when the doc does not have the section yet. Writing this
            # heading into a body that ALREADY has Ben's notes elsewhere makes
            # upsert_managed skip its preservation branch — his notes would be
            # replaced by this empty stub. See obsidian.upsert_managed.
            lines += ["", NOTES_HEADING, "", "_(Anything you write here is preserved.)_"]
        return "\n".join(lines)

    async def _persist_state(
        self, project: Project, run: dict, observed: dict, plan: dict
    ) -> None:
        signature = _digest(
            "|".join(
                str(x) for x in (
                    observed["git"].get("last_commit_at"),
                    observed["open_task_count"],
                    observed["attention_score"],
                    observed["idle_days"],
                )
            )
        )[:16]
        now = _now()
        try:
            state = await self.db[PLANS_COLLECTION].find_one({"_id": project.slug}) or {}
        except Exception:
            state = {}
        progressed = state.get("progress_signature") != signature
        # Streak is measured in DAYS since the last observed change, not in
        # ticks: at a 30-minute cadence a per-tick counter would read 48 after
        # one quiet day, and §6.3's "no_progress_streak >= 3" means three days.
        last_progress = now if progressed else (_as_utc(state.get("last_progress_at")) or now)
        streak = int((now - last_progress).total_seconds() // 86400)

        try:
            await self.db[PLANS_COLLECTION].update_one(
                {"_id": project.slug},
                {"$set": {
                    "slug": project.slug,
                    "plan_path": plan.get("path") or observed["plan"]["path"],
                    "plan_hash": plan.get("hash"),
                    "body_hash": plan.get("body_hash"),
                    "last_run_at": now,
                    "last_status": run["status"],
                    "progress_signature": signature,
                    "last_progress_at": last_progress or now,
                    "approval": observed["plan"].get("approval"),
                }},
                upsert=True,
            )
        except Exception as exc:
            logger.debug("steward: plan state write failed for %s: %s", project.slug, exc)

        # Worker-owned bookkeeping only — update_steward_state rejects anything
        # outside StewardState, so this cannot reach `status` or `charter`.
        try:
            await self.planning.update_steward_state(
                project.slug,
                {
                    "enabled": True,
                    "last_run_at": now,
                    "plan_hash": plan.get("hash"),
                    "last_report_ref": plan.get("path"),
                    "no_progress_streak": streak,
                },
            )
        except Exception as exc:
            logger.debug("steward: steward-state write failed for %s: %s", project.slug, exc)

        note = self._activity_note(run)
        if note:
            try:
                await self.planning.append_project_activity(
                    project.id, source="steward", note=note
                )
            except Exception as exc:
                logger.debug("steward: activity append failed for %s: %s", project.slug, exc)

    def _activity_note(self, run: dict) -> Optional[str]:
        """One line for the project log — and nothing at all on a quiet tick.

        A 30-minute worker that logs every tick evicts the 20-entry activity
        ring in ten hours, which would erase exactly the human-written history
        the cockpit exists to show.
        """
        if run["actions_executed"]:
            return "steward: " + "; ".join(
                f"{a['kind']}: {_clip(a['title'], 80)}" for a in run["actions_executed"]
            )
        if run.get("pause_proposal", {}).get("proposed"):
            return f"steward: proposed pausing this project ({run['pause_proposal']['reason']})"
        if not (run.get("model") or {}).get("ok") and not (run.get("model") or {}).get("skipped"):
            return f"steward: could not assess ({_clip((run.get('model') or {}).get('error'), 100)})"
        return None

    async def _record_run(self, run: dict) -> None:
        try:
            await self.db[RUNS_COLLECTION].insert_one(dict(run))
        except Exception as exc:
            logger.warning("steward: could not record run for %s: %s", run.get("slug"), exc)

    async def _notify(self, *, event_type: str, detail: str, **kwargs) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.notify(source="steward", event_type=event_type,
                                       detail=detail, **kwargs)
        except Exception as exc:  # pragma: no cover
            logger.warning("steward: alert failed (%s): %s", event_type, exc)

    # --------------------------------------------------------- vault events

    async def handle_vault_events(self, events: list[dict]) -> dict:
        """Apply what Ben changed in the vault. Called by VaultReader.on_events.

        BEN'S EDIT ALWAYS WINS: every write below goes through a human-owned
        path with `actor="human"`, and nothing here writes a field back into the
        vault. An event this method cannot attribute to a project is filed for
        review rather than dropped — a charter typed on a phone that matched no
        project is the one failure Ben would never see otherwise.
        """
        results: list[dict] = []
        for event in events or []:
            kind = event.get("type")
            handler = {
                "charter": self._on_charter,
                "autonomy": self._on_autonomy,
                "approval": self._on_approval,
                "accepted": self._on_accepted,
                "notes": self._on_notes,
                "human_edit": self._on_human_edit,
                "parse_error": self._on_bad_doc,
                "invalid_value": self._on_bad_doc,
                "too_large": self._on_bad_doc,
            }.get(kind)
            if handler is None:
                results.append({"type": kind, "action": "ignored"})
                continue
            try:
                outcome = await handler(event)
            except Exception as exc:
                logger.warning("steward: vault event %s failed: %s", kind, exc, exc_info=True)
                outcome = {"action": "error", "error": f"{type(exc).__name__}: {exc}"}
            results.append({"type": kind, "path": event.get("rel_path"), **outcome})
        if results:
            logger.info("steward: applied %d vault event(s)", len(results))
        return {"handled": len(results), "results": results}

    async def _resolve_project(self, event: dict) -> Optional[Project]:
        """vault/<Folder>/… -> the project whose repo basename is <Folder>."""
        folder = (event.get("project") or "").strip()
        if not folder:
            return None
        for project in await self.planning.list_projects():
            base = os.path.basename((project.path or "").rstrip("/"))
            if base and base.lower() == folder.lower():
                return project
        by_slug = await self.planning.get_project_by_slug(folder.lower())
        if by_slug:
            return by_slug
        return await self.planning.fuzzy_find_project(folder)

    async def _unmatched(self, event: dict, what: str) -> dict:
        folder = event.get("project") or "?"
        try:
            from aria.shared.review import add_review_item

            await add_review_item(
                self.db,
                kind="steward_vault_unmatched",
                subject=f"{folder}/{event.get('rel_path')}",
                detail=f"{what} in the vault matches no project (folder '{folder}')",
                source="steward",
            )
        except Exception as exc:
            logger.debug("steward: could not file unmatched vault event: %s", exc)
        logger.info("steward: vault %s for unknown project folder '%s'", what, folder)
        return {"action": "unmatched", "folder": folder}

    async def _on_charter(self, event: dict) -> dict:
        project = await self._resolve_project(event)
        if project is None:
            return await self._unmatched(event, "charter")
        raw = event.get("value")
        if not isinstance(raw, dict):
            raw = event.get("frontmatter") or {}
        # Filter to charter fields here rather than letting set_charter warn per
        # unknown key: a CHARTER.md's frontmatter also carries title/created/
        # updated, and a warning per doc key per poll is noise, not signal.
        patch = {k: v for k, v in raw.items() if k in set(Charter.model_fields)}
        if not patch:
            return {"action": "no-op", "reason": "no charter fields in frontmatter"}
        updated = await self.planning.set_charter(
            project.slug, patch, actor="human", via="vault"
        )
        if updated is None:
            return {"action": "error", "error": "project vanished"}
        await self.planning.append_project_activity(
            project.id, source="vault",
            note=f"charter updated from the vault: {', '.join(sorted(patch))}",
        )
        return {"action": "charter_applied", "slug": project.slug, "fields": sorted(patch)}

    async def _on_autonomy(self, event: dict) -> dict:
        project = await self._resolve_project(event)
        if project is None:
            return await self._unmatched(event, "autonomy change")
        value = event.get("value")
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3:
            return {"action": "rejected", "reason": f"autonomy {value!r} out of range"}
        await self.planning.set_charter(
            project.slug, {"autonomy": value}, actor="human", via="vault"
        )
        await self.planning.append_project_activity(
            project.id, source="vault",
            note=f"autonomy set to {AUTONOMY_NAMES.get(value, value)} from the vault",
        )
        return {"action": "autonomy_applied", "slug": project.slug, "autonomy": value}

    async def _on_approval(self, event: dict) -> dict:
        project = await self._resolve_project(event)
        if project is None:
            return await self._unmatched(event, "approval")
        value = event.get("value")
        now = _now()
        try:
            await self.db[PLANS_COLLECTION].update_one(
                {"_id": project.slug},
                {"$set": {
                    "slug": project.slug,
                    "approval": value,
                    "approval_at": now,
                    "approval_via": "vault",
                    "approval_path": event.get("path"),
                }},
                upsert=True,
            )
        except Exception as exc:
            logger.warning("steward: approval write failed for %s: %s", project.slug, exc)
        await self.planning.append_project_activity(
            project.id, source="vault", note=f"steward plan marked '{value}'",
        )
        # Informational, not a raise: Ben just made this decision himself.
        await self._notify(
            event_type="plan_approval",
            detail=f"{project.slug}: plan {value} (from the vault)",
            severity=SEVERITY_INFO, needs_human=False,
            project_slug=project.slug, project_path=project.path,
            dedup_key=f"steward|approval|{project.slug}",
        )
        return {"action": "approval_recorded", "slug": project.slug, "approval": value}

    async def _on_accepted(self, event: dict) -> dict:
        """Ben's verdict on a research note (§5 step 5) — the accepted-artifact
        metric, and the planner's topic weighting, both read this."""
        value = event.get("value")
        path = event.get("path")
        frontmatter = event.get("frontmatter") or {}
        query = {"$or": [{"vault_path": path}]}
        if frontmatter.get("topic_hash"):
            query["$or"].append({"topic_hash": frontmatter["topic_hash"]})
        if frontmatter.get("research_id"):
            query["$or"].append({"_id": frontmatter["research_id"]})
        try:
            run = await self.db.research_runs.find_one(query)
        except Exception as exc:
            logger.debug("steward: research run lookup failed: %s", exc)
            run = None
        if not run:
            return {"action": "unmatched_research", "path": path}
        try:
            await self.db.research_runs.update_one(
                {"_id": run["_id"]},
                {"$set": {"accepted": value, "accepted_at": _now(), "accepted_via": "vault"}},
            )
        except Exception as exc:
            return {"action": "error", "error": str(exc)}
        return {"action": "research_accepted", "research_id": str(run["_id"]), "accepted": value}

    async def _on_notes(self, event: dict) -> dict:
        """`## Notes from Ben` is an instruction to the next tick, so it is kept
        where the next tick's prompt reads it."""
        project = await self._resolve_project(event)
        if project is None:
            return await self._unmatched(event, "notes")
        try:
            await self.db[PLANS_COLLECTION].update_one(
                {"_id": project.slug},
                {"$set": {
                    "slug": project.slug,
                    "notes_from_ben": _clip(event.get("value"), 4000),
                    "notes_at": _now(),
                }},
                upsert=True,
            )
        except Exception as exc:
            logger.warning("steward: notes write failed for %s: %s", project.slug, exc)
        await self.planning.append_project_activity(
            project.id, source="vault", note="Ben left notes on the steward plan",
        )
        return {"action": "notes_recorded", "slug": project.slug}

    async def _on_human_edit(self, event: dict) -> dict:
        project = await self._resolve_project(event)
        if project is None:
            return {"action": "ignored", "reason": "no project"}
        try:
            await self.db[PLANS_COLLECTION].update_one(
                {"_id": project.slug},
                {"$set": {"slug": project.slug, "last_human_edit_at": _now(),
                          "last_human_edit_path": event.get("path")}},
                upsert=True,
            )
        except Exception as exc:
            logger.debug("steward: human-edit note failed: %s", exc)
        return {"action": "noted", "slug": project.slug}

    async def _on_bad_doc(self, event: dict) -> dict:
        """A control doc ARIA could not read means Ben's edit did NOT take
        effect. He is the only one who can fix it, and he cannot fix what he is
        not told about — so this is one of the few steward events that raises."""
        detail = (
            f"{event.get('rel_path')}: {event.get('error') or event.get('reason') or 'unreadable'} — "
            "this edit has NOT been applied"
        )
        await self._notify(
            event_type="vault_doc_unreadable",
            detail=detail,
            needs_human=True, severity="medium", kind="steward",
            dedup_key=f"steward|vault_bad_doc|{event.get('path')}",
            cooldown_seconds=3600,
        )
        return {"action": "raised", "detail": detail}

    # --------------------------------------------------------------- read API

    async def status(self) -> dict:
        try:
            projects = await self.planning.active_projects()
        except Exception as exc:
            projects = []
            logger.debug("steward status: active set unreadable: %s", exc)
        rows = []
        for project in projects:
            state = {}
            try:
                state = await self.db[PLANS_COLLECTION].find_one({"_id": project.slug}) or {}
            except Exception:
                pass
            charter = project.charter or Charter()
            rows.append({
                "slug": project.slug,
                "name": project.name,
                "path": project.path,
                "autonomy": int(charter.autonomy or 0),
                "autonomy_label": AUTONOMY_NAMES.get(int(charter.autonomy or 0)),
                "approval": state.get("approval"),
                "last_run_at": state.get("last_run_at"),
                "last_status": state.get("last_status"),
                "plan_path": state.get("plan_path"),
                "paused_reason": getattr(project.steward, "paused_reason", None),
                "budget": effective_budget(charter),
            })
        return {
            "enabled": settings.steward_enabled,
            "running": self._task is not None and not self._task.done(),
            "interval_minutes": settings.steward_interval_minutes,
            "max_actions_per_tick": settings.steward_max_actions_per_tick,
            "model": {
                "backend": settings.steward_backend,
                "model": settings.steward_model,
                "endpoint": settings.steward_endpoint,
                "max_tokens": settings.steward_max_tokens,
            },
            "ticks": self.ticks,
            "last_tick": self.last_tick,
            "active_projects": rows,
            # Said explicitly because it is today's state and it is not a fault:
            # no charter exists yet, so there is nothing to steward.
            "note": None if rows else (
                "No chartered projects. The active set is status=active AND "
                "kind=project AND a charter with a purpose."
            ),
        }

    async def recent_runs(self, *, slug: Optional[str] = None, limit: int = 20) -> list[dict]:
        query = {"slug": slug} if slug else {}
        try:
            docs = await self.db[RUNS_COLLECTION].find(query).sort(
                "started_at", -1
            ).limit(int(limit)).to_list(length=int(limit))
        except Exception as exc:
            logger.warning("steward: run history unreadable: %s", exc)
            return []
        for doc in docs:
            doc["id"] = str(doc.pop("_id", ""))
        return docs

    async def resume(self, slug: str) -> Optional[dict]:
        """Clear a stand-down so the steward works the project again.

        `propose_pause` is a one-way door without this: it sets
        `steward.paused_reason`, and every later tick skips the project on that
        field — so the answer to the question it asked needs a way in.
        """
        project = await self.planning.get_project_by_ident(slug)
        if project is None:
            return None
        await self.planning.update_steward_state(slug, {"paused_reason": None})
        await self.planning.append_project_activity(
            project.id, source="steward", note="steward resumed by an operator",
        )
        return {"slug": slug, "paused_reason": None}


class _NoShells:
    """Stand-in used when this process has no shells substrate."""

    async def fleet_overview(self) -> list:
        return []
