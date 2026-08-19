"""
ARIA - Planning Models (tasks + projects)

Pydantic shapes for the to-do list and long-term project tracker. Tasks have
a lifecycle (proposed -> active -> done | dismissed); projects are coarse
groupings the user works on with a rolling activity log.

Projects also carry the steward layer (proposal §4): `kind` separates a project
from scratch/ignored inventory, `charter` says why the project exists (human-
owned), and `steward` is ARIA's own bookkeeping (worker-owned). The three
together define the ACTIVE SET — the only projects the steward acts on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field


TaskStatus = Literal["proposed", "active", "done", "dismissed"]
ProjectStatus = Literal["active", "paused", "archived"]
TaskSourceType = Literal["manual", "conversation", "shell", "awareness", "import"]
# WHO is expected to do this — not who filed it (`source` answers that).
#
# ARIA is a headless Linux box: it cannot hold a phone, listen to audio, or sit
# in a meeting. A steward proposal like "verify the iOS and Android client fixes
# on real devices with a no-look voice playtest" is a legitimate next action and
# an impossible one for any agent here, and with no owner it arrives looking
# like something the system intends to do. `unknown` is the default so the
# thousands of pre-existing rows are not silently relabelled as either.
TaskOwner = Literal["agent", "human", "unknown"]
# `kind` separates "a directory on disk" from "a thing Ben works on". Before it
# existed the harvester registered 59 rows — Downloads, /tmp/workspace, venv,
# the Obsidian vault, .worktrees/* and pi smoke dirs included — all status=active,
# which is why the cockpit's attention ranking was noise.
ProjectKind = Literal["project", "scratch", "ignored"]
CharterVia = Literal["vault", "api", "mcp"]


class TaskSource(BaseModel):
    """Where a task came from. `type=manual` for user-created; everything else
    is ambient extraction."""
    type: TaskSourceType
    conversation_id: Optional[str] = None
    shell_name: Optional[str] = None
    message_ids: Optional[list[str]] = None
    extracted_at: Optional[datetime] = None
    confidence: Optional[float] = None


class Task(BaseModel):
    """A to-do item. Persisted in the `tasks` collection."""
    id: str
    title: str
    notes: Optional[str] = None
    status: TaskStatus = "active"
    owner: TaskOwner = "unknown"
    due_at: Optional[datetime] = None
    project_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    source: TaskSource
    content_hash: str  # sha256 of normalized title; used for hash-based dedup
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


class TaskCreateRequest(BaseModel):
    """Manual task creation."""
    title: str = Field(..., min_length=1, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=4000)
    due_at: Optional[datetime] = None
    project_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    status: TaskStatus = "active"
    owner: TaskOwner = "unknown"


class TaskUpdateRequest(BaseModel):
    """Partial update for a task."""
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=4000)
    status: Optional[TaskStatus] = None
    owner: Optional[TaskOwner] = None
    due_at: Optional[datetime] = None
    project_id: Optional[str] = None
    tags: Optional[list[str]] = None


class TaskListResponse(BaseModel):
    tasks: list[Task]


class ProjectActivity(BaseModel):
    """One entry in a project's recent_activity log (capped, FIFO-evicted)."""
    at: datetime
    source: str  # e.g. "conversation:<id>", "manual", "shell:<name>"
    note: str


class CharterCadence(BaseModel):
    """How often the steward looks at this project, and how often it researches.
    Free-form strings (`30m`, `daily`, `weekly`, `manual`) parsed by the steward,
    not here — the schema must not have to change to add a cadence."""
    steward: str = "30m"
    research: str = "weekly"


class CharterBudget(BaseModel):
    """Per-project spend caps. Every field is Optional and *unset means unset* —
    defaults come from `settings.steward_default_*` via
    `planning.service.effective_budget()`, so Ben can retune the fleet-wide
    defaults in config without rewriting every charter."""
    sessions_per_day: Optional[int] = Field(default=None, ge=0)
    session_minutes: Optional[int] = Field(default=None, ge=1)
    local_tokens_per_day: Optional[int] = Field(default=None, ge=0)
    cloud_usd_per_day: Optional[float] = Field(default=None, ge=0)
    research_runs_per_week: Optional[int] = Field(default=None, ge=0)
    lines_merge: Optional[int] = Field(default=None, ge=0)


class CharterGuard(BaseModel):
    """Per-project blast radius. `protected_paths` is additive to the fleet-wide
    `settings.guard_protected_paths` — a charter can narrow what an agent may
    touch, never widen it."""
    allowed_paths: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    merge_gate: Literal["check_command", "tests", "none"] = "check_command"
    reviewer_family: Literal["cloud", "qwen"] = "cloud"


class Charter(BaseModel):
    """Why a project exists — the input every steward and research decision keys
    off. HUMAN-OWNED under S3: a worker may propose changes (they land in
    `db.scan_review`) but never writes this field.

    Every field is optional so a partial charter is valid; the *active set* only
    requires `purpose` to be non-empty, because a purpose is the minimum a
    research question or a steward plan can be derived from.

    `autonomy` — what the steward may do on this project without asking:
      A0 observe   — harvest, cockpit, digest lines only. The default.
      A1 propose   — write STEWARD_PLAN.md, propose tasks/topics, run research
                     (read-only web + vault writes). No coding sessions.
      A2 execute   — spawn coding sessions ONLY in a sandboxed worktree on
                     aria/<project>/<sid8>; the merge gate must pass and the
                     merge itself is *proposed* to Ben. Local models cap here.
      A3 auto-merge — squash-merge behind the full gate (gate green +
                     different-family review + allowed_paths + lines_merge).
                     Cloud tier only, per-project opt-in, never for protected
                     paths.
    """
    purpose: str = ""
    goals: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    research_topics: list[str] = Field(default_factory=list)
    autonomy: int = Field(default=0, ge=0, le=3)
    tiers_allowed: list[str] = Field(default_factory=list)  # local|ridge|red|cloud
    cadence: CharterCadence = Field(default_factory=CharterCadence)
    budget: CharterBudget = Field(default_factory=CharterBudget)
    guard: CharterGuard = Field(default_factory=CharterGuard)
    approved_at: Optional[datetime] = None
    approved_via: Optional[CharterVia] = None


class StewardState(BaseModel):
    """The steward's own bookkeeping for a project. WORKER-owned — the human
    lifecycle is `Project.status`, and nothing here may change it.

    `paused_reason` is the steward standing down on this project (budget
    exhausted, ladder exhausted, pause proposed and pending); the project stays
    `status=active` until Ben decides."""
    enabled: bool = False
    last_run_at: Optional[datetime] = None
    plan_hash: Optional[str] = None  # hash of the last STEWARD_PLAN.md ARIA wrote
    last_report_ref: Optional[str] = None
    no_progress_streak: int = 0
    paused_reason: Optional[str] = None


class Project(BaseModel):
    """A long-running effort the user is working on."""
    id: str
    name: str
    slug: str
    summary: str = ""
    status: ProjectStatus = "active"
    next_steps: list[str] = Field(default_factory=list)  # rolling, max ~5
    relevant_paths: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    recent_activity: list[ProjectActivity] = Field(default_factory=list)  # capped at 20
    created_at: datetime
    updated_at: datetime
    last_signal_at: Optional[datetime] = None
    # Coherence C1 Verification Gate: the shell command the watchdog runs in
    # this project's workspace before honoring a Ralph-loop session's done
    # token. None -> falls back to the server-wide coding_gate_command
    # default ("make check"); a project with no usable check either way is
    # skipped, not blocked.
    check_command: Optional[str] = None

    # --- Steward layer (proposal §4). `kind` is set by the harvester on insert
    # and by a human thereafter; `charter` is human-only; `steward` is
    # worker-only. The active set the steward iterates is
    # status=active AND kind=project AND charter.purpose non-empty.
    #
    # ⚠️ `steward` is Optional in the SCHEMA but must never be persisted as an
    # explicit null: it is written with dotted paths (`steward.last_run_at`),
    # and MongoDB 8.2 refuses to create a field under a null parent — "Cannot
    # create field 'paused_reason' in element {steward: null}" (reproduced
    # against the live mongod, 2026-08-15). Writers store `{}`; see
    # `planning.service._steward_set`.
    kind: ProjectKind = "project"
    charter: Optional[Charter] = None
    steward: Optional[StewardState] = None

    # --- Derived fields, written by the project harvester (never hand-edited).
    # `status` above stays the human lifecycle (active/paused/archived); machine
    # activity lives in `activity_status` so the two never collide.
    path: Optional[str] = None  # primary canonical path (git toplevel when available)
    last_activity_at: Optional[datetime] = None
    activity_status: Optional[Literal["active", "idle"]] = None
    sources: list[dict] = Field(default_factory=list)  # provenance: git/claude/pi/shells
    git: Optional[dict] = None  # {branch, last_commit_at, last_commit_subject}
    harvested_at: Optional[datetime] = None


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    slug: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="URL-safe identifier; auto-derived from name if omitted",
    )
    summary: str = Field(default="", max_length=2000)
    status: ProjectStatus = "active"
    next_steps: list[str] = Field(default_factory=list)
    relevant_paths: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    check_command: Optional[str] = None
    kind: ProjectKind = "project"
    charter: Optional[Charter] = None


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    summary: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[ProjectStatus] = None
    next_steps: Optional[list[str]] = None
    relevant_paths: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    check_command: Optional[str] = None
    kind: Optional[ProjectKind] = None
    # A charter sent here is MERGED, not replaced (PlanningService.update_project
    # routes it through set_charter): a vault or phone edit legitimately carries
    # only the keys that changed, and a plain $set would blank the rest.
    # `steward` is deliberately absent — it is worker-owned state.
    charter: Optional[Charter] = None


class CharterSetRequest(BaseModel):
    """Body of PUT /projects/{ident}/charter. The charter is a PARTIAL patch —
    only the keys actually present are merged. `via` records the approval
    surface (D10: the vault copy is the approval source, the API and MCP are
    equals); the actor is always human, because this route IS a human surface —
    workers call PlanningService.set_charter() in-process with their own actor
    and get proposal-not-write semantics."""
    charter: Charter = Field(default_factory=Charter)
    via: CharterVia = "api"


class CharterResponse(BaseModel):
    """GET/PUT /projects/{ident}/charter. `effective_budget` is the resolved
    view (charter values over settings.steward_default_*) so a caller never has
    to re-derive the defaults.

    `in_active_set` / `active_set_blockers` exist because writing a charter and
    getting a 200 back used to say nothing about whether the steward would ever
    look at the project — a charter on a `kind=ignored` row was accepted, echoed
    and then ignored forever. The blockers name the failing condition
    (status / kind / empty purpose) in the same response.
    """
    project_id: str
    slug: str
    kind: ProjectKind
    charter: Optional[Charter] = None
    steward: Optional[StewardState] = None
    effective_budget: dict
    in_active_set: bool = False
    active_set_blockers: list[str] = Field(default_factory=list)


class ProjectListResponse(BaseModel):
    projects: list[Project]
