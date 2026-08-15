"""
ARIA - Planning Service (tasks + projects)

CRUD for the to-do list and project tracker, plus the dedup helpers used by
the ambient TaskExtractor. Intentionally thin: the service owns persistence
shape and idempotency rules; the extractor owns the LLM call.

Also owns the charter/active-set rules (proposal §4): a charter is human-owned
(a worker proposes into `db.scan_review`, never writes), budgets resolve against
`settings.steward_default_*` rather than being baked into the schema, and the
project lifecycle (draft -> active -> paused -> archived) stays human-owned —
the steward may only *propose* a pause.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.planning.models import (
    Charter,
    Project,
    ProjectActivity,
    ProjectCreateRequest,
    ProjectStatus,
    ProjectUpdateRequest,
    StewardState,
    Task,
    TaskCreateRequest,
    TaskSource,
    TaskStatus,
    TaskUpdateRequest,
)
from aria.shared.ownership import merge_owned
from aria.shared.review import add_review_item

logger = logging.getLogger(__name__)

# Caps to prevent unbounded growth from ambient updates.
MAX_RECENT_ACTIVITY = 20
MAX_NEXT_STEPS = 5
# Tasks with these statuses are "open" — dedup checks against this set so a
# completed task with the same title can be re-created.
OPEN_STATUSES: tuple[TaskStatus, ...] = ("proposed", "active")

# Actors permitted to WRITE a charter. Everything else — the steward, the
# harvester, a coding agent reporting back — can only propose (S3, and proposal
# principle 12: the thing being evaluated must not be able to rewrite its own
# objective). Kept as a set rather than a bool flag so the provenance written
# into `charter.source.<field>.actor` stays meaningful.
CHARTER_HUMAN_ACTORS = frozenset({"human", "ben", "vault", "api", "mcp"})
# Charter sub-objects that must merge KEY-BY-KEY. A vault or phone edit sends
# only the keys that changed ({"budget": {"sessions_per_day": 1}}); a shallow
# merge would blank the other five budget fields.
CHARTER_NESTED_FIELDS = ("cadence", "budget", "guard")
CHARTER_PROPOSAL_KIND = "charter_proposal"
PAUSE_PROPOSAL_KIND = "project_pause_proposal"
CHARTER_REFUSED_KIND = "charter_kind_conflict"
# Mirrors shells.harvest.HARVEST_ACTOR. Duplicated rather than imported because
# `aria.shells.__init__` pulls in ShellService; test_charter asserts the two
# stay equal. It is how set_charter tells "a glob classified this row" from
# "a human classified this row" — the two deserve opposite answers.
HARVEST_ACTOR = "project-harvester"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_title(title: str) -> str:
    """Lowercase, collapse whitespace, strip terminal punctuation. Used for
    hash-based dedup so trivial wording differences don't create duplicates."""
    s = title.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.rstrip(" .!?;,:")
    return s


def _content_hash(title: str) -> str:
    return hashlib.sha256(_normalize_title(title).encode("utf-8")).hexdigest()


def _slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "project"


def _safe_object_id(value: str) -> Optional[ObjectId]:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def effective_budget(charter: Optional[Charter | dict]) -> dict:
    """Resolve a charter's budget against the fleet-wide defaults.

    The defaults deliberately live in `settings.steward_default_*` (decision
    D13) and NOT in `CharterBudget`: retuning what an unattended project may
    spend must be a config change Ben can make in one place, not a migration
    over every charter document. An unset field here therefore means "use the
    current default", not "unlimited".

    `local_tokens_per_day` has no configured default on purpose — local tokens
    are free in dollars and the real constraint is slot contention (Qwen slot 2
    prefill), which the steward enforces by scheduling, not by a token count.
    None means uncapped.
    """
    if isinstance(charter, Charter):
        budget = charter.budget.model_dump()
    elif isinstance(charter, dict):
        budget = dict((charter.get("budget") or {}))
    else:
        budget = {}

    def pick(field: str, default):
        value = budget.get(field)
        return default if value is None else value

    return {
        "sessions_per_day": pick("sessions_per_day", settings.steward_default_sessions_per_day),
        "session_minutes": pick("session_minutes", settings.steward_default_session_minutes),
        "local_tokens_per_day": budget.get("local_tokens_per_day"),
        "cloud_usd_per_day": pick("cloud_usd_per_day", settings.steward_default_cloud_usd_per_day),
        "research_runs_per_week": pick(
            "research_runs_per_week", settings.steward_default_research_runs_per_week
        ),
        "lines_merge": pick("lines_merge", settings.steward_default_lines_merge),
    }


class CharterRefused(Exception):
    """A human charter that cannot take effect on this project, refused loudly
    instead of being stored where nothing will ever read it.

    The failure this exists for: a charter written to a `kind=ignored` row was
    accepted, echoed back with a 200, and then never seen again — the steward
    only iterates kind=project, so the charter was a no-op with a success
    response. Silence is the one thing this layer must not do; either the row
    is promoted (a glob classified it) or the caller is told why not (a human
    classified it) and how to resolve it.
    """

    def __init__(self, slug: str, reason: str, remedy: str):
        self.slug = slug
        self.reason = reason
        self.remedy = remedy
        super().__init__(f"{slug}: {reason} — {remedy}")


def active_set_blockers(project: Project) -> list[str]:
    """Why `project` is NOT in the steward's active set. Empty list = it is.

    The single definition of the active set, read by `active_projects()` (which
    filters on it) and by the charter response (which reports it), so the set
    the steward iterates and the set a human is told about can never drift.
    """
    blockers: list[str] = []
    if project.status != "active":
        blockers.append(f"status={project.status}, needs status=active")
    if project.kind != "project":
        blockers.append(f"kind={project.kind}, needs kind=project")
    if not (project.charter and project.charter.purpose.strip()):
        blockers.append("charter.purpose is empty — the steward has nothing to act on")
    return blockers


def _steward_set(doc: dict, patch: dict) -> dict:
    """Build the `$set` operand for a write to the worker-owned steward state.

    Steward bookkeeping is addressed with dotted paths (`steward.plan_hash`),
    and MongoDB cannot create a field under a NULL parent. Verified against the
    live mongod 8.2.0:

        {"$set": {"steward.paused_reason": ...}} on {steward: null}
        -> WriteError: Cannot create field 'paused_reason' in element {steward: null}

    A MISSING parent auto-creates, which is the only reason this never fired on
    the 59 legacy rows. Rows that already carry an explicit null (anything
    written by create_project/harvest before this was fixed) are healed here by
    replacing the whole sub-document, so the first steward tick repairs them
    instead of raising.
    """
    stored = doc.get("steward")
    if "steward" in doc and not isinstance(stored, dict):
        return {"steward": dict(patch)}
    return {f"steward.{key}": value for key, value in patch.items()}


def _merge_charter(existing: dict, patch: dict) -> dict:
    """Deep-merge a partial charter patch over the stored charter.

    Nested budget/guard/cadence objects merge key-by-key (see
    CHARTER_NESTED_FIELDS); lists and scalars replace, because "the goals are
    now these three" has to be expressible."""
    merged = dict(existing)
    for field, value in patch.items():
        if field in CHARTER_NESTED_FIELDS and isinstance(value, dict):
            base = dict(merged.get(field) or {})
            base.update({k: v for k, v in value.items()})
            merged[field] = base
        else:
            merged[field] = value
    return merged


class PlanningService:
    """Tasks + projects persistence. Methods are concurrency-safe (Mongo
    handles concurrent writes); dedup is best-effort, not transactional."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.tasks = db.tasks
        self.projects = db.projects

    # ---------------------------------------------------------------- helpers
    def _task_from_doc(self, doc: dict) -> Task:
        doc = dict(doc)
        doc["id"] = str(doc.pop("_id"))
        if doc.get("project_id") is not None and not isinstance(doc["project_id"], str):
            doc["project_id"] = str(doc["project_id"])
        return Task(**doc)

    def _project_from_doc(self, doc: dict) -> Project:
        doc = dict(doc)
        doc["id"] = str(doc.pop("_id"))
        return Project(**doc)

    # ---------------------------------------------------------- task CRUD
    async def create_task(
        self,
        body: TaskCreateRequest,
        *,
        source: Optional[TaskSource] = None,
    ) -> Task:
        """Create a task. `source` defaults to manual when omitted."""
        now = _now()
        src = source or TaskSource(type="manual")
        doc = {
            "title": body.title.strip(),
            "notes": body.notes,
            "status": body.status,
            "due_at": body.due_at,
            "project_id": body.project_id,
            "tags": list(body.tags),
            "source": src.model_dump(),
            "content_hash": _content_hash(body.title),
            "created_at": now,
            "updated_at": now,
            "completed_at": now if body.status == "done" else None,
        }
        result = await self.tasks.insert_one(doc)
        doc["_id"] = result.inserted_id
        return self._task_from_doc(doc)

    async def list_tasks(
        self,
        *,
        status: Optional[list[TaskStatus]] = None,
        project_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[Task]:
        query: dict = {}
        if status:
            query["status"] = {"$in": list(status)}
        if project_id:
            query["project_id"] = project_id
        cursor = self.tasks.find(query).sort([("status", 1), ("updated_at", -1)]).limit(int(limit))
        return [self._task_from_doc(doc) async for doc in cursor]

    async def get_task(self, task_id: str) -> Optional[Task]:
        oid = _safe_object_id(task_id)
        if oid is None:
            return None
        doc = await self.tasks.find_one({"_id": oid})
        return self._task_from_doc(doc) if doc else None

    async def update_task(self, task_id: str, body: TaskUpdateRequest) -> Optional[Task]:
        oid = _safe_object_id(task_id)
        if oid is None:
            return None
        update: dict = body.model_dump(exclude_unset=True)
        if not update:
            return await self.get_task(task_id)
        update["updated_at"] = _now()
        if "title" in update:
            update["title"] = update["title"].strip()
            update["content_hash"] = _content_hash(update["title"])
        if update.get("status") == "done":
            update["completed_at"] = _now()
        elif "status" in update and update["status"] != "done":
            update["completed_at"] = None
        await self.tasks.update_one({"_id": oid}, {"$set": update})
        return await self.get_task(task_id)

    async def set_task_status(self, task_id: str, status: TaskStatus) -> Optional[Task]:
        return await self.update_task(task_id, TaskUpdateRequest(status=status))

    async def delete_task(self, task_id: str) -> bool:
        oid = _safe_object_id(task_id)
        if oid is None:
            return False
        result = await self.tasks.delete_one({"_id": oid})
        return result.deleted_count > 0

    # ---------------------------------------------------------- project CRUD
    async def create_project(self, body: ProjectCreateRequest) -> Project:
        now = _now()
        slug = body.slug or _slugify(body.name)
        # Slug uniqueness — if collision, suffix with -2, -3, ...
        base = slug
        suffix = 1
        while await self.projects.find_one({"slug": slug}):
            suffix += 1
            slug = f"{base}-{suffix}"
        doc = {
            "name": body.name.strip(),
            "slug": slug,
            "summary": body.summary,
            "status": body.status,
            "next_steps": list(body.next_steps)[:MAX_NEXT_STEPS],
            "relevant_paths": list(body.relevant_paths),
            "tags": list(body.tags),
            "check_command": body.check_command,
            "kind": body.kind,
            # A hand-created project with a charter is approved at creation —
            # the creator is a human surface (the harvester never calls this).
            "charter": (
                {
                    **body.charter.model_dump(),
                    "approved_at": body.charter.approved_at or now,
                    "approved_via": body.charter.approved_via or "api",
                    "source": {"purpose": {"actor": "human", "at": now}},
                }
                if body.charter is not None
                else None
            ),
            # An EMPTY DOCUMENT, never null. `steward` is written with dotted
            # paths and MongoDB refuses to create a field under a null parent
            # ("Cannot create field 'no_progress_streak' in element {steward:
            # null}", live mongod 8.2.0) — persisting null here made every
            # newly created project permanently unusable by the steward and
            # raised on its first tick. `charter` stays null because it is only
            # ever written wholesale (set_charter replaces the field).
            "steward": {},
            "recent_activity": [],
            "created_at": now,
            "updated_at": now,
            "last_signal_at": None,
        }
        # Provenance for `kind`, but only when the creator actually chose one.
        # An explicit kind here is a human decision — the harvester must not
        # reclassify it (_reconcile_kind) and set_charter must not silently
        # promote it away. The ambient extractor never passes `kind`, so the
        # rows it creates stay harvester-reclassifiable.
        if "kind" in body.model_fields_set:
            doc["source"] = {"kind": {"actor": "human", "at": now}}
        result = await self.projects.insert_one(doc)
        doc["_id"] = result.inserted_id
        return self._project_from_doc(doc)

    async def list_projects(self, *, status: Optional[ProjectStatus] = None) -> list[Project]:
        query: dict = {}
        if status:
            query["status"] = status
        cursor = self.projects.find(query).sort([("status", 1), ("last_signal_at", -1), ("updated_at", -1)])
        return [self._project_from_doc(doc) async for doc in cursor]

    async def get_project(self, project_id: str) -> Optional[Project]:
        oid = _safe_object_id(project_id)
        if oid is None:
            return None
        doc = await self.projects.find_one({"_id": oid})
        return self._project_from_doc(doc) if doc else None

    async def get_project_by_slug(self, slug: str) -> Optional[Project]:
        doc = await self.projects.find_one({"slug": slug})
        return self._project_from_doc(doc) if doc else None

    async def update_project(self, project_id: str, body: ProjectUpdateRequest) -> Optional[Project]:
        # ObjectId OR slug, like every other project entry point. It used to be
        # id-only, which made the one documented way to change `kind`
        # unreachable for callers that hold a slug — MCP and the vault address
        # projects by slug and nothing else.
        doc = await self._find_project_doc(project_id)
        if doc is None:
            return None
        oid = doc["_id"]
        update = body.model_dump(exclude_unset=True)
        if not update:
            return self._project_from_doc(doc)
        if "next_steps" in update and update["next_steps"] is not None:
            update["next_steps"] = update["next_steps"][:MAX_NEXT_STEPS]
        # A charter arriving on a PATCH goes through the merge path, not $set:
        # PATCH bodies are partial by definition, and a $set of a partial
        # charter would blank every key the caller happened not to send.
        # An explicit `charter: null` is the one exception — that is a human
        # deliberately retiring the charter, so it clears the field.
        has_charter = "charter" in update
        update.pop("charter", None)
        # The rest of the PATCH lands FIRST, and `kind` rides in it. That
        # ordering is the escape hatch for a row a human marked `ignored`:
        # {"kind": "project", "charter": {...}} in one PATCH resolves the
        # contradiction before set_charter looks at it, instead of the caller
        # having to make two calls to get past the refusal.
        if update:
            update["updated_at"] = _now()
            await self.projects.update_one({"_id": oid}, {"$set": update})
        if has_charter:
            if body.charter is None:
                await self.projects.update_one(
                    {"_id": oid}, {"$set": {"charter": None, "updated_at": _now()}}
                )
            else:
                return await self.set_charter(
                    project_id, body.charter.model_dump(exclude_unset=True), actor="human", via="api"
                )
        return await self._reread(oid)

    async def delete_project(self, project_id: str) -> bool:
        oid = _safe_object_id(project_id)
        if oid is None:
            return False
        result = await self.projects.delete_one({"_id": oid})
        # Detach orphaned tasks (don't delete them — user may still want them)
        await self.tasks.update_many(
            {"project_id": project_id}, {"$set": {"project_id": None, "updated_at": _now()}}
        )
        return result.deleted_count > 0

    async def append_project_activity(
        self, project_id: str, *, source: str, note: str
    ) -> bool:
        """Push a new activity entry, capped at MAX_RECENT_ACTIVITY (FIFO)."""
        oid = _safe_object_id(project_id)
        if oid is None:
            return False
        entry = ProjectActivity(at=_now(), source=source, note=note).model_dump()
        result = await self.projects.update_one(
            {"_id": oid},
            {
                "$push": {
                    "recent_activity": {
                        "$each": [entry],
                        "$slice": -MAX_RECENT_ACTIVITY,
                    }
                },
                "$set": {"last_signal_at": _now(), "updated_at": _now()},
            },
        )
        return result.modified_count > 0

    async def set_project_next_steps(self, project_id: str, steps: list[str]) -> bool:
        oid = _safe_object_id(project_id)
        if oid is None:
            return False
        steps = [s.strip() for s in steps if s and s.strip()][:MAX_NEXT_STEPS]
        await self.projects.update_one(
            {"_id": oid},
            {"$set": {"next_steps": steps, "updated_at": _now()}},
        )
        return True

    # ------------------------------------------------- charters / active set
    async def _find_project_doc(self, ident: str) -> Optional[dict]:
        """Resolve an ObjectId *or* a slug. Agents address projects by slug (it
        is the stable name); the UI has the id."""
        oid = _safe_object_id(ident)
        if oid is not None:
            doc = await self.projects.find_one({"_id": oid})
            if doc:
                return doc
        return await self.projects.find_one({"slug": ident})

    async def get_project_by_ident(self, ident: str) -> Optional[Project]:
        doc = await self._find_project_doc(ident)
        return self._project_from_doc(doc) if doc else None

    async def _reread(self, doc_id) -> Optional[Project]:
        """Re-read by the raw _id we already hold — never by str(_id), which
        would go back through the ObjectId-or-slug resolver and lose any id
        shape that is not a 24-hex ObjectId."""
        doc = await self.projects.find_one({"_id": doc_id})
        return self._project_from_doc(doc) if doc else None

    async def set_charter(
        self,
        ident: str,
        charter: dict,
        *,
        actor: str = "human",
        via: str = "api",
    ) -> Optional[Project]:
        """Merge a partial charter into a project. Returns None if no such
        project.

        S3 ownership: the charter is HUMAN-owned. An actor outside
        CHARTER_HUMAN_ACTORS never writes it — the proposal is queued into
        `db.scan_review` and the stored charter is returned unchanged. This is
        principle 12 in the proposal: an agent that can rewrite its own charter
        can rewrite its own budget, autonomy level and allowed paths, which is
        exactly the failure shape documented in DGM / AI Scientist / METR.

        The patch is merged, never substituted, because the vault is the
        approval surface (D10) and a phone edit carries only what changed.
        """
        if via not in ("vault", "api", "mcp"):
            raise ValueError(f"invalid charter approval surface: {via!r}")
        doc = await self._find_project_doc(ident)
        if doc is None:
            return None

        slug = doc.get("slug") or str(doc.get("_id"))
        known = set(Charter.model_fields)
        patch = {k: v for k, v in (charter or {}).items() if k in known}
        ignored = set(charter or {}) - known
        if ignored:
            logger.warning("set_charter(%s): ignoring unknown field(s) %s", slug, sorted(ignored))

        if actor not in CHARTER_HUMAN_ACTORS:
            # Propose, don't clobber. Deduped by (kind, subject) while unacked,
            # so a worker re-proposing every tick is one review row, not a flood.
            await add_review_item(
                self.db,
                kind=CHARTER_PROPOSAL_KIND,
                subject=slug,
                detail=f"{actor} proposes charter change: {sorted(patch)} — {patch}"[:2000],
                source=actor,
            )
            logger.info("set_charter(%s): %s is not a human actor — proposed for review", slug, actor)
            return self._project_from_doc(doc)

        now = _now()
        existing = dict(doc.get("charter") or {})
        merged = _merge_charter(existing, patch)
        # A human touching the charter IS the approval event, so stamp it —
        # unless the caller supplied its own (the VaultReader replays the
        # frontmatter's own approved_at rather than inventing a new one).
        if "approved_at" not in patch:
            merged["approved_at"] = now
        if "approved_via" not in patch:
            merged["approved_via"] = via
        final = Charter(**merged).model_dump()

        # Per-field provenance, S3 shape (`source.<field>.actor`). Conflicts are
        # structurally impossible on this branch — a human owns every charter
        # field, and every non-human actor returned above — so merge_owned is
        # used here only for the provenance shape the review surface reads.
        # NB: the project doc's `sources` (plural) is the harvester's discovery
        # provenance — different field, different meaning; do not merge them.
        owned_set, _ = merge_owned(existing, patch, worker_fields=set(patch), actor=actor)
        provenance = dict(existing.get("source") or {})
        provenance.update(owned_set.get("source") or {})
        final["source"] = provenance
        final["last_verified_at"] = now

        update: dict = {"charter": final, "updated_at": now}
        # A human writing a purpose is the statement that this IS a project;
        # without this the charter would sit silently outside the active set.
        #
        # `ignored` gets promoted too when the harvester put it there. Almost
        # every ignored row is a glob match, not a decision: HARVEST_IGNORE_NAMES
        # applies basename globs (`session-*`, `*-wt`, `*-smoke.*`) to every path
        # on the box, and all 18 ignored rows live on 2026-08-15 carry
        # source.kind.actor=project-harvester. Treating a glob as a human "no"
        # is what made `infrastructure/rocmfpx-decode-fusion-wt` uncharterable.
        # A human-set ignore IS a "no" and is refused loudly below — never
        # stored-and-forgotten, which is the bug class this layer exists to kill.
        kind = doc.get("kind")
        kind_actor = ((doc.get("source") or {}).get("kind") or {}).get("actor")
        if final.get("purpose") and kind != "project":
            if kind == "ignored" and kind_actor not in (None, HARVEST_ACTOR):
                reason = (
                    f"kind=ignored was set by {kind_actor}, not by the harvester's "
                    "ignore globs — a chartered project must be kind=project, and "
                    "one human decision must not silently overwrite another"
                )
                remedy = (
                    f"PATCH /api/v1/projects/{slug} with kind=project first "
                    "(kind and charter may travel in the same PATCH — the kind is "
                    "applied before the charter)"
                )
                await add_review_item(
                    self.db,
                    kind=CHARTER_REFUSED_KIND,
                    subject=slug,
                    detail=f"charter refused ({actor} via {via}): {reason}"[:2000],
                    source=actor,
                )
                logger.warning("set_charter(%s): refused — %s", slug, reason)
                raise CharterRefused(slug, reason, remedy)
            update["kind"] = "project"
            update["source.kind"] = {"actor": actor, "at": now}
        update["source.charter"] = {"actor": actor, "at": now, "via": via}

        await self.projects.update_one({"_id": doc["_id"]}, {"$set": update})
        return await self._reread(doc["_id"])

    async def active_projects(self, *, include_stood_down: bool = True) -> list[Project]:
        """THE ACTIVE SET — the only projects the steward acts on:
        status=active AND kind=project AND a charter with a non-empty purpose
        (the three conditions live in `active_set_blockers`, so this and the
        charter response can never disagree about who is in the set).

        Everything else in `projects` is inventory. The purpose test is not
        cosmetic: it is the text every research question and steward plan is
        derived from, so a charter without one gives the steward nothing to act
        on and would produce generic busywork.

        `include_stood_down=False` also drops projects carrying
        `steward.paused_reason`. That field is the steward's own stand-down
        (budget exhausted, ladder exhausted, pause proposed and pending) and
        propose_pause promises it "stops it iterating this project" — but
        `status` stays human-owned and therefore still `active`, so the
        definitional set contains them. A worker that iterates and spends must
        pass False; a human-facing listing wants the default.

        Filtered in Python rather than in the query because rows predating
        `kind` have no such field at all (all 59 of them, 2026-08-15) and must
        read as projects, not vanish.
        """
        cursor = self.projects.find({"status": "active"})
        out: list[Project] = []
        async for doc in cursor:
            try:
                proj = self._project_from_doc(doc)
            except Exception as exc:  # a malformed row must not blind the steward
                logger.warning("active_projects: skipping unparseable project %s: %s", doc.get("slug"), exc)
                continue
            if active_set_blockers(proj):
                continue
            if not include_stood_down and proj.steward and proj.steward.paused_reason:
                continue
            out.append(proj)
        return out

    async def propose_pause(self, ident: str, reason: str) -> bool:
        """Record a proposal to pause a project. NEVER sets `status`.

        The lifecycle (draft -> active -> paused -> archived) is human-owned;
        the steward standing down is recorded in `steward.paused_reason`, which
        stops it iterating this project while Ben's decision is pending.
        """
        doc = await self._find_project_doc(ident)
        if doc is None:
            return False
        slug = doc.get("slug") or str(doc.get("_id"))
        await add_review_item(
            self.db,
            kind=PAUSE_PROPOSAL_KIND,
            subject=slug,
            detail=f"steward proposes pausing '{slug}': {reason}"[:2000],
            source="steward",
        )
        update = _steward_set(doc, {"paused_reason": reason})
        update["updated_at"] = _now()
        await self.projects.update_one({"_id": doc["_id"]}, {"$set": update})
        return True

    async def update_steward_state(self, ident: str, patch: dict) -> Optional[Project]:
        """Worker-owned bookkeeping (last_run_at, plan_hash, streaks). Rejects
        anything outside StewardState so a caller cannot smuggle `status` or
        `charter` in through the steward's own write path."""
        doc = await self._find_project_doc(ident)
        if doc is None:
            return None
        known = set(StewardState.model_fields)
        accepted = {k: v for k, v in (patch or {}).items() if k in known}
        if not accepted:
            return self._project_from_doc(doc)
        update = _steward_set(doc, accepted)
        update["updated_at"] = _now()
        await self.projects.update_one({"_id": doc["_id"]}, {"$set": update})
        return await self._reread(doc["_id"])

    # --------------------------------------------------- ambient/dedup helpers
    async def find_open_task_by_hash(self, content_hash: str) -> Optional[Task]:
        """Find an existing open task with the same normalized title."""
        doc = await self.tasks.find_one(
            {"content_hash": content_hash, "status": {"$in": list(OPEN_STATUSES)}}
        )
        return self._task_from_doc(doc) if doc else None

    async def fuzzy_find_project(self, hint: str) -> Optional[Project]:
        """Match a free-text hint to an existing project by exact slug or
        case-insensitive substring on name/slug. Returns at most one match
        (the most recently active one); returns None if no match — callers
        should NOT auto-create projects from hints in v1."""
        if not hint:
            return None
        slug_hint = _slugify(hint)
        # Exact slug first
        doc = await self.projects.find_one({"slug": slug_hint})
        if doc:
            return self._project_from_doc(doc)
        # Substring on name or slug
        regex = {"$regex": re.escape(hint.strip()), "$options": "i"}
        doc = await self.projects.find_one(
            {"$or": [{"name": regex}, {"slug": regex}]},
            sort=[("last_signal_at", -1), ("updated_at", -1)],
        )
        return self._project_from_doc(doc) if doc else None
