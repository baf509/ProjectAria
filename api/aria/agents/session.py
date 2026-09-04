"""
ARIA - Coding Session Manager

Purpose: Start, stop, and inspect coding-agent subprocess sessions.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.agents.backends.base import CommandSpec, StartParams
from aria.agents.backends.registry import BackendRegistry
from aria.agents.backends.registry import CodingBackendUnavailableError
from aria.agents.backends.tmux import TmuxManager
from aria.agents.checkpoint import (
    build_resume_prompt,
    find_resumable_checkpoint,
    write_checkpoint,
)
from aria.agents.mail import AgentMailbox, MessageType
from aria.agents.subprocess_mgr import CodingSubprocessManager
from aria.guard.gitguard import GuardGitError, get_git_guard
from aria.guard.policy import record_event
from aria.guard.sandbox import build_sandbox_prefix, preflight, resource_prefix, session_env
from aria.infrastructure.git_worktree import WorktreeError
from aria.infrastructure.git_worktree import create_worktree as _create_worktree
from aria.shells.service import ShellService
from aria.config import settings
from aria.notifications.service import NotificationService

import logging

logger = logging.getLogger(__name__)


def _git_repo_root(path: str) -> Optional[str]:
    """The git work-tree root containing `path`, or None if there isn't one.

    Deliberately NOT `ensure_repo()`: `guard_worktree_default` turns the
    worktree on for every session, and a default that silently `git init`s
    whatever directory it was pointed at (~/Downloads, /tmp/scratch, a mounted
    share) would create repos nobody asked for — and a repo ARIA just created
    has no history to roll back to, so it buys no safety either. A workspace
    that is not a repo therefore gets no worktree (recorded as a guard event),
    while an EXPLICIT create_worktree=True still initialises one, exactly as
    before this change.

    A path already inside `<repo>/.worktrees/<name>` resolves to the repo, not
    to the linked worktree: otherwise resuming a session in a worktree would
    nest `.worktrees/` one level deeper every time.
    """
    if not os.path.isdir(path):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    root = proc.stdout.strip()
    if proc.returncode != 0 or not root:
        return None
    marker = os.sep + ".worktrees" + os.sep
    if marker in root + os.sep:
        return root.split(marker)[0]
    return root


def resolve_active_session_manager(explicit=None):
    """The coding-session manager this process is actually using.

    The killswitch and the e-stop have to reach it to stop RUNNING sessions
    (proposal §7.3 — until now they only blocked new spawns), but both are
    constructed long before the session manager and neither may edit
    `api/aria/api/deps.py` from here. Explicit wiring (`set_coding_manager`)
    is the supported path; the deps singleton is the fallback so the stop
    actually happens on the live service before that wiring lands. Returns
    None when no manager exists, which is the correct answer in tests.
    """
    if explicit is not None:
        return explicit
    try:
        from aria.api import deps

        return getattr(deps, "_coding_session_manager", None)
    except Exception:  # pragma: no cover - import-time failure must not block a stop
        return None


class CodingSessionManager:
    """Manage coding sessions backed by external CLI agents."""

    def __init__(self, db: AsyncIOMotorDatabase, notification_service: NotificationService | None = None):
        self.db = db
        self.registry = BackendRegistry()
        self.process_manager = CodingSubprocessManager()
        self.tmux_manager = TmuxManager() if TmuxManager.is_available() else None
        # Shell substrate: ARIA-spawned coding sessions run as watched shells so
        # they unify with the fleet (auto-adopt, capture, same drive/observe tools).
        self.shell_service = ShellService(db) if settings.shells_enabled else None
        self.mailbox = AgentMailbox(db)
        self.notification_service = notification_service
        # Deployment routing policy is captured with the manager just like the
        # concurrency limits below. This keeps one coherent policy for the
        # manager's lifetime and makes explicit host=None tests independent of
        # whichever operator .env happens to be loaded on the test machine.
        configured_host = getattr(settings, "coding_default_host", "")
        self._default_host = configured_host.strip() if isinstance(configured_host, str) else ""
        self._watch_tasks: dict[str, asyncio.Task] = {}
        # Periodic guard checkpoints, one task per guarded session. ARIA makes
        # the commit (proposal principle 11) — an agent that can skip its own
        # checkpoint has no checkpoint, and before this the `session_checkpoints`
        # collection had never been written at all.
        self._checkpoint_tasks: dict[str, asyncio.Task] = {}
        # Global concurrency limiter (Pi-Flow parity). A session holds a "slot"
        # while it is actively running; spawns beyond the cap wait in a `queued`
        # state until a slot frees. Slot bookkeeping is guarded by a Condition
        # and keyed by session_id in `_slotted`, so release is idempotent across
        # the many finalize paths (watch tasks, stop, deferred-launch failures).
        self._slot_limit: int = int(settings.coding_max_concurrent_sessions or 0)
        self._slot_cv = asyncio.Condition()
        self._active: int = 0
        self._slotted: set[str] = set()
        # Second, narrower limit: backends whose underlying model server has
        # its own hard single-consumer ceiling, independent of ARIA's global
        # cap above. Two concurrent sessions on one of these would queue or
        # evict at the INFERENCE layer instead of ARIA's own queue:
        #   - laguna (backend `pool`) holds ONE coding slot in the unified KV
        #     pool -- a second concurrent session evicts the first's prefix
        #     and forces a full re-prefill (measured 2026-07-27: ~220s for a
        #     30k prefix).
        #   - Ridge (backend `ridge`, NInfer) has no continuous batching --
        #     "one request at a time, concurrent callers queue"
        #     (docs/ops/LOCAL_INFERENCE_TOPOLOGY.md §3.1).
        # Cloud backends (claude_code, codex) have neither constraint and are
        # bounded only by the global cap above. Keyed by canonical backend
        # name so adding a third such backend is one line, not four new
        # methods.
        self._backend_limits: dict[str, int] = {
            "pool": int(settings.coding_max_concurrent_laguna_sessions or 0),
            "ridge": int(settings.coding_max_concurrent_ridge_sessions or 0),
            # Local llama.cpp slots on the resident server. Without this, the
            # global cap is the wrong instrument: it counts claude_code
            # sessions, which cost zero local capacity, against pi sessions,
            # which each hold a slot for their whole life.
            "pi-code": int(settings.coding_max_concurrent_pi_sessions or 0),
        }
        self._backend_slotted: dict[str, set[str]] = {
            name: set() for name in self._backend_limits
        }

    def _use_shell_substrate(self) -> bool:
        return bool(settings.coding_use_shell_substrate and self.shell_service)

    # ------------------------------------------------------------------
    # Concurrency slots
    # ------------------------------------------------------------------
    def _limited_backend(self, backend: str | None) -> str | None:
        """Canonical backend name if it has its own narrower slot limit
        (see `_backend_limits`), else None."""
        try:
            canon = self.registry.canonicalize(backend)
        except Exception:
            return None
        return canon if canon in self._backend_limits else None

    def _slot_free(self, limited_backend: str | None = None) -> bool:
        if self._slot_limit > 0 and self._active >= self._slot_limit:
            return False
        if limited_backend:
            limit = self._backend_limits.get(limited_backend, 0)
            if limit > 0 and len(self._backend_slotted[limited_backend]) >= limit:
                return False
        return True

    async def _try_acquire_slot_nowait(self, session_id: str, backend: str | None = None) -> bool:
        """Reserve a concurrency slot iff one is free right now. Atomic w.r.t.
        other slot ops (single event loop + Condition lock). Returns False at
        capacity — the caller should queue instead."""
        async with self._slot_cv:
            if session_id in self._slotted:
                return True
            limited = self._limited_backend(backend)
            if self._slot_free(limited):
                self._active += 1
                self._slotted.add(session_id)
                if limited:
                    self._backend_slotted[limited].add(session_id)
                return True
            return False

    async def _acquire_slot(self, session_id: str, backend: str | None = None) -> None:
        """Block until a concurrency slot is free, then hold it (idempotent)."""
        async with self._slot_cv:
            if session_id in self._slotted:
                return
            limited = self._limited_backend(backend)
            while not self._slot_free(limited):
                await self._slot_cv.wait()
            self._active += 1
            self._slotted.add(session_id)
            if limited:
                self._backend_slotted[limited].add(session_id)

    async def _release_slot(self, session_id: str) -> None:
        """Release a held slot (idempotent) and wake one waiter."""
        async with self._slot_cv:
            if session_id in self._slotted:
                self._slotted.discard(session_id)
                for slotted in self._backend_slotted.values():
                    slotted.discard(session_id)
                self._active = max(0, self._active - 1)
                # notify_all, not notify(1): with independent per-backend
                # limits a single woken waiter may still be blocked on
                # another one and would otherwise consume the wakeup,
                # stalling an eligible waiter.
                self._slot_cv.notify_all()

    async def concurrency_stats(self) -> dict:
        """Live limiter gauge: currently-running (slot-holding) sessions, how
        many are queued waiting for a slot, the configured global cap (0 =
        off), and per-backend-family usage (pool/ridge — each's own
        single-consumer ceiling, independent of the global cap; see
        `_backend_limits`). A future cockpit/TUI surface can render "1/1
        active, N queued" per backend from `backends` directly."""
        queued = await self.db.coding_sessions.count_documents({"status": "queued"})
        backends = {
            name: {"active": len(self._backend_slotted[name]), "limit": limit}
            for name, limit in self._backend_limits.items()
        }
        return {
            "active": self._active,
            "queued": queued,
            "limit": self._slot_limit,
            "backends": backends,
        }

    @staticmethod
    def _normalize_loop_config(raw: dict) -> dict:
        """Fill a Ralph-loop config with settings defaults so the stored doc is
        self-describing (what you read is what the watchdog runs)."""
        raw = raw or {}
        return {
            "nudge_prompt": raw.get("nudge_prompt") or settings.coding_loop_nudge_prompt,
            "nudge_prompt_file": raw.get("nudge_prompt_file") or None,
            "idle_seconds": int(raw.get("idle_seconds") or settings.coding_loop_idle_seconds),
            "done_regex": raw.get("done_regex") or settings.coding_loop_done_regex,
            "max_nudges": int(raw.get("max_nudges") or settings.coding_loop_max_nudges),
            "deadline_minutes": int(
                raw.get("deadline_minutes") or settings.coding_loop_deadline_minutes
            ),
            "notify_every": int(raw.get("notify_every") or 0),
            # Verification Gate (C1) per-session overrides. None here means
            # "use the project's check_command, or the global default" — the
            # watchdog resolves that chain, not this normalizer, since the
            # project lookup needs a DB call this staticmethod can't make.
            "gate_command": raw.get("gate_command") or None,
            "gate_timeout": int(raw.get("gate_timeout") or settings.coding_gate_timeout_seconds),
            "gate_max_retries": int(raw.get("gate_max_retries") or settings.coding_gate_max_retries),
        }

    async def set_loop_config(
        self, session_id: str, config: Optional[dict]
    ) -> Optional[dict]:
        """Enable (config dict) or disable (None) the Ralph nudge loop on an
        existing session. Enabling resets the nudge counter + deadline clock."""
        session = await self.get_session(session_id)
        if not session:
            return None
        now = datetime.now(timezone.utc)
        updates: dict = {"updated_at": now}
        if config is None:
            updates["loop_config"] = None
        else:
            updates["loop_config"] = self._normalize_loop_config(config)
            updates["loop_nudges"] = 0
            updates["last_nudge_at"] = None
            updates["loop_started_at"] = now
            updates["gate_failures"] = 0
        await self.db.coding_sessions.update_one({"_id": session_id}, {"$set": updates})
        return await self.get_session(session_id)

    async def start_session(
        self,
        *,
        workspace: str,
        backend: Optional[str],
        prompt: str,
        branch: Optional[str] = None,
        model: Optional[str] = None,
        llm: Optional[str] = None,
        conversation_id: Optional[str] = None,
        visible: bool = False,
        loop: Optional[dict] = None,
        host: Optional[str] = None,
        subagent_profile: Optional[str] = None,
        # None (not False) is the default so "the caller said nothing" and "the
        # caller said no" stay distinguishable: unspecified follows
        # settings.guard_worktree_default (D15 — worktree by default for every
        # ARIA-spawned session, every backend), while an explicit False still
        # runs in the live checkout for the callers that mean it.
        create_worktree: Optional[bool] = None,
        worktree_name: Optional[str] = None,
    ) -> dict:
        # Safety gates: refuse to spawn an autonomous coding agent while the
        # manual killswitch or the automated emergency stop is engaged.
        # Fail CLOSED — a verification error denies the spawn.
        from aria.api.deps import get_killswitch, resolve_estop_manager
        get_killswitch().check_or_raise("coding session start")
        estop = await resolve_estop_manager(self.db)
        if await estop.is_active():
            state = await estop.get_state()
            raise RuntimeError(
                f"Emergency stop active — coding session start blocked. Reason: {state.reason}"
            )

        # A deployment can pin unattended work to an OS-isolated fleet node.
        # Explicit callers still win, including tests and operator overrides.
        host = host or self._default_host or None

        # The session id is minted HERE, before anything else, because the guard
        # keys its worktree, branch, start tag and checkpoint commits off it.
        session_id = str(uuid4())

        # Guard seam, part 1 of 2 (proposal §7.2/§7.3): decide, refuse, but do
        # not touch git yet. The refusals belong here, before any work — the
        # worktree itself is cut further down, after the arguments have been
        # validated, so a bad `subagent_profile` or a disabled backend cannot
        # leave an orphan branch + worktree + mirror push behind.
        guard = await self._guard_plan(
            session_id=session_id,
            workspace=workspace,
            host=host,
            create_worktree=create_worktree,
            worktree_name=worktree_name,
        )

        # Declarative specialist profile (Pi-Flow subagents parity): resolve a
        # named `db.agents` row and apply it — its llm pins backend/model (an
        # explicit arg still wins), and its system_prompt becomes the role
        # preamble to the task. A profile-pinned model skips complexity routing,
        # since choosing the specialist IS choosing its model.
        profile = None
        profile_role = None
        if subagent_profile:
            profile = await self.db.agents.find_one({"slug": subagent_profile}) or \
                await self.db.agents.find_one({"name": subagent_profile})
            if not profile:
                raise RuntimeError(f"subagent profile '{subagent_profile}' not found")
            pllm = profile.get("llm", {}) or {}
            profile_backend = pllm.get("backend")
            if backend is None:
                # profile_backend is a Pi provider/LLM-adapter name (llamacpp,
                # agentic, ridge, ...) — a different vocabulary from the
                # coding-session process backend. Non-process backends select
                # the real external Pi CLI plus its explicit provider.
                if self.registry.is_registered(profile_backend):
                    backend = profile_backend
                else:
                    backend = "pi-code"
                    llm = llm or profile_backend
            if model is None:
                model = pllm.get("model")
            role = profile.get("system_prompt")
            if role:
                profile_role = role

        # Complexity routing: with no model pinned, classify the task and run it
        # on the tier's model — planning/design on Opus, scoped work on Sonnet.
        # An explicit `model` always wins, as does an explicit backend the router
        # wouldn't have picked itself (see `is_routable_backend`). Routing only
        # fills the gap, and a failure falls through to the configured defaults.
        # Normalise aliases BEFORE anything compares `backend` to a canonical
        # name. is_routable_backend() tests membership in {"claude_code"}, so a
        # caller passing the "claude-code" alias used to skip complexity routing
        # silently — no error, just the CLI default model instead of the routed
        # tier. Canonicalising here fixes it for every downstream check at once.
        backend = self.registry.canonicalize(backend)

        routing_meta = None
        if model is None and settings.coding_routing_enabled:
            try:
                from aria.agents.routing import (
                    ComplexityRouter,
                    QuotaCooldownError,
                    is_routable_backend,
                )

                if is_routable_backend(backend):
                    verdict = await ComplexityRouter(self.db).classify(
                        prompt, workspace=workspace
                    )
                    backend = verdict.backend
                    model = verdict.model
                    llm = verdict.llm or llm
                    routing_meta = verdict.to_meta()
                    logger.info(
                        "routed coding task -> %s/%s (tier=%s, %s)",
                        verdict.backend, verdict.model, verdict.tier, verdict.why,
                    )
            except QuotaCooldownError:
                # Deliberately NOT swallowed like other routing failures: with no
                # fallback configured, an exhausted Claude quota must stop the
                # spawn rather than quietly run the task on a weaker model.
                raise
            except Exception as exc:
                logger.warning("complexity routing failed (%s); using defaults", exc)

        backend_name = backend or settings.coding_default_backend
        if backend_name == "pool" and not settings.pool_enabled:
            raise RuntimeError(
                "pool backend is disabled (settings.pool_enabled=False) -- "
                "the underlying model server is down. Not attempting to connect."
            )
        selected_backend = self.registry.get(backend_name)
        workspace_path = os.path.abspath(workspace)

        # A bare pi-code request inherits the local Pi profile. Named profiles
        # already supplied these fields above. The db.agents row is launch
        # configuration only here: no ARIA conversation/orchestrator loop is
        # created; the external Pi process owns its own transcript and tools.
        if backend_name == "pi-code" and (not llm or not model):
            pi_profile = profile or await self.db.agents.find_one({"slug": "pi-coding"})
            if not pi_profile:
                raise RuntimeError("Pi coding profile not found (slug='pi-coding'); run migrations.")
            pi_llm = pi_profile.get("llm", {}) or {}
            llm = llm or pi_llm.get("backend")
            model = model or pi_llm.get("model")
        if backend_name == "pi-code" and (not llm or not model):
            raise ValueError("pi-code requires a configured provider and model")

        if not host:
            await self._preflight_local_backend(backend_name)

        # Non-Pi coding backends historically received a specialist role as a
        # preamble to the task. Pi has a native system-prompt append flag, so
        # keep role instructions in the correct message channel there.
        if profile_role and backend_name != "pi-code":
            prompt = f"{profile_role}\n\n---\n\nTask:\n{prompt}"

        # Guard seam, part 2 of 2: cut the worktree. Everything that could
        # refuse this spawn has already run, so from here a worktree that gets
        # created is a worktree that gets used.
        await self._guard_provision(guard, session_id)
        source_repo = guard.get("source_repo")
        if guard.get("worktree"):
            # The session runs in the worktree, not the live checkout.
            # Everything downstream (StartParams, the pi-code/tmux/subprocess
            # dispatches, the watchdog, review) only ever sees this path.
            workspace_path = guard["worktree"]
            if branch is None:
                branch = guard.get("branch")

        # Which backends get the OS sandbox is a per-backend policy knob, and
        # `backend_name` is only final after routing — so the decision is made
        # here, recorded on the session, and read back at launch.
        guard["sandbox"] = bool(
            guard.get("active")
            and settings.guard_sandbox_enabled
            and backend_name in (settings.guard_sandbox_backends or [])
        )

        params = StartParams(
            workspace=workspace_path,
            prompt=prompt,
            model=model,
            branch=branch,
            provider=llm if backend_name == "pi-code" else None,
            append_system_prompt=profile_role if backend_name == "pi-code" else None,
            session_id=session_id if backend_name == "pi-code" else None,
        )
        command = selected_backend.start_command(params)
        now = datetime.now(timezone.utc)

        loop_config = self._normalize_loop_config(loop) if loop else None
        doc = {
            "_id": session_id,
            "backend": backend_name,
            "llm": llm if backend_name == "pi-code" else None,
            "model": model,
            "workspace": workspace_path,
            "source_repo": source_repo,
            # What the guard did for this session, so the watchdog, the merge
            # gate and a human reading the doc can all tell a guarded session
            # from an unguarded one without re-deriving it.
            "guard": {
                "active": bool(guard.get("active")),
                "worktree": guard.get("worktree"),
                "repo": guard.get("source_repo"),
                "branch": guard.get("branch"),
                "start_tag": guard.get("start_tag"),
                "mirror": guard.get("mirror"),
                "sandbox": bool(guard.get("sandbox")),
                "degraded": guard.get("degraded"),
            },
            "prompt": prompt,
            "branch": branch,
            "conversation_id": conversation_id,
            "visible": visible,
            # `queued` until a concurrency slot is acquired; the launch flips it
            # to `running`. With a free slot this is instantaneous (fast path).
            "status": "queued",
            "pid": None,
            "tmux_pane_id": None,
            "shell_name": None,
            # Which machine this session runs on (None = this host). A remote
            # host runs the session on its aria-node; drive/get_output dispatch
            # by the shell's host, so the watchdog + Ralph loop work over the wire.
            "host": host,
            "node_id": host if host else None,
            # Ralph loop bookkeeping (loop_config None = not looping).
            "loop_config": loop_config,
            "loop_nudges": 0,
            "last_nudge_at": None,
            "loop_started_at": now if loop_config else None,
            # How the model was chosen (None = explicitly pinned by the caller).
            "routing": routing_meta,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        await self.db.coding_sessions.insert_one(doc)

        # Concurrency gate: hold a slot for the session's active life. If one is
        # free now, launch inline (unchanged fast path). Otherwise leave the doc
        # `queued` and launch from a background waiter when a slot frees.
        resource_backend = llm if backend_name == "pi-code" else backend_name
        if await self._try_acquire_slot_nowait(session_id, resource_backend):
            try:
                return await self._launch_substrate(
                    session_id, command, backend_name, workspace_path, visible, host, prompt,
                    guard=guard,
                )
            except Exception:
                await self._release_slot(session_id)
                raise

        if settings.coding_queue_max > 0:
            queued = await self.db.coding_sessions.count_documents({"status": "queued"})
            if queued > settings.coding_queue_max:
                now = datetime.now(timezone.utc)
                await self.db.coding_sessions.update_one(
                    {"_id": session_id},
                    {"$set": {
                        "status": "failed",
                        "error": f"coding queue full ({queued} > {settings.coding_queue_max})",
                        "updated_at": now,
                        "completed_at": now,
                    }},
                )
                raise RuntimeError(
                    f"coding queue full ({queued} > {settings.coding_queue_max}) — session refused"
                )
        self._watch_tasks[session_id] = asyncio.create_task(
            self._deferred_launch(
                session_id, command, backend_name, resource_backend,
                workspace_path, visible, host, prompt, guard
            )
        )
        logger.info(
            "Queued coding session %s (limit=%s, active=%s)",
            session_id, self._slot_limit, self._active,
        )
        return await self.get_session(session_id)

    async def _preflight_local_backend(self, backend_name: str) -> None:
        """Fail before provisioning when a local coding CLI cannot launch.

        Remote capability belongs to the node. Locally, binary/auth failures
        are cheap to identify and should be structured API errors rather than a
        disposable tmux session Hermes has to inspect after the fact.
        """
        binary = {
            "claude_code": settings.claude_code_binary,
            "codex": settings.codex_binary,
            "pi-code": settings.pi_coding_binary,
            "pool": settings.pool_binary,
        }.get(backend_name)
        if not binary:
            return
        if os.path.isabs(binary):
            # Configuration is shared across Mac/Corsair deployments. An
            # absolute path valid on the other OS may be present here; resolve
            # the same executable name on this host before declaring it absent.
            resolved = binary if os.access(binary, os.X_OK) else shutil.which(os.path.basename(binary))
        else:
            resolved = shutil.which(binary)
        if not resolved:
            raise CodingBackendUnavailableError(
                backend_name, f"executable not found: {binary}", retryable=False
            )
        if backend_name != "claude_code":
            return

        def claude_auth_status() -> tuple[bool, str]:
            try:
                completed = subprocess.run(
                    [resolved, "auth", "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, f"auth preflight failed: {exc}"
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "not authenticated").strip()
                return False, detail[:300]
            try:
                status = json.loads(completed.stdout)
            except (ValueError, TypeError):
                return False, "auth status returned invalid JSON"
            if not status.get("loggedIn"):
                return False, "Claude CLI is not logged in"
            return True, ""

        ok, reason = await asyncio.to_thread(claude_auth_status)
        if not ok:
            raise CodingBackendUnavailableError(
                backend_name, reason, retryable=True
            )

    async def _deferred_launch(
        self, session_id, command, backend_name, resource_backend,
        workspace_path, visible, host, prompt, guard=None
    ) -> None:
        """Background waiter for a queued session: block for a slot, re-check the
        safety gates (a stop may have engaged while queued — fail closed), then
        launch the substrate. `_launch_substrate` installs the real watch task,
        which owns the slot release."""
        try:
            await self._acquire_slot(session_id, resource_backend)
            from aria.api.deps import get_killswitch, resolve_estop_manager
            try:
                get_killswitch().check_or_raise("queued coding session start")
                estop = await resolve_estop_manager(self.db)
                if await estop.is_active():
                    state = await estop.get_state()
                    raise RuntimeError(f"Emergency stop active — {state.reason}")
            except Exception as exc:
                now = datetime.now(timezone.utc)
                await self.db.coding_sessions.update_one(
                    {"_id": session_id},
                    {"$set": {"status": "failed", "error": str(exc),
                              "updated_at": now, "completed_at": now}},
                )
                await self._release_slot(session_id)
                return
            # Honour a stop issued while queued (status no longer `queued`).
            cur = await self.get_session(session_id)
            if not cur or cur.get("status") != "queued":
                await self._release_slot(session_id)
                return
            await self._launch_substrate(
                session_id, command, backend_name, workspace_path, visible, host, prompt,
                guard=guard,
            )
        except asyncio.CancelledError:
            await self._release_slot(session_id)
            raise
        except Exception as exc:
            logger.warning("deferred launch failed for %s: %s", session_id, exc)
            await self._release_slot(session_id)

    # ------------------------------------------------------------------
    # Guard seam (proposal §7.2 git protocol, §7.3 machine)
    # ------------------------------------------------------------------
    async def _guard_plan(
        self,
        *,
        session_id: str,
        workspace: str,
        host: Optional[str],
        create_worktree: Optional[bool],
        worktree_name: Optional[str],
    ) -> dict:
        """Decide what the guard does for this session. NO side effects.

        Returns the guard context the rest of the launch reads. `active` means
        the guard governs this spawn (preflight ran, the resource scope
        applies); `worktree` is filled in later by `_guard_provision`.

        The refusal lives here, before anything has been created:
        preflight failing (the sandbox is requested and bwrap is missing or
        cannot start a process, or MemAvailable is under the floor — a spawn
        under it OOM-kills a resident model) means no session at all. FAIL
        CLOSED: "just this once, unsandboxed" is how a safety control becomes
        decorative.

        Scope of the gate: the guard refuses the spawns it GOVERNS. A workspace
        that is not a git repo gets no worktree (see `_git_repo_root`) and, with
        the sandbox off, nothing else to enforce — refusing those too would take
        the whole coding surface down for a control with no rollback story
        there. That is recorded as a guard event, not swallowed.
        """
        ctx: dict = {
            "active": False,
            "source_repo": None,
            "worktree": None,
            "branch": None,
            "start_tag": None,
            "mirror": None,
            "sandbox": False,
            "preflight": None,
            "degraded": None,
            # Plan fields, consumed by _guard_provision.
            "workspace": os.path.abspath(workspace),
            "repo_root": None,
            "will_worktree": False,
            "explicit": create_worktree is not None,
            "worktree_name": worktree_name,
            "legacy": False,
        }

        from aria.nodes import is_remote_host

        if is_remote_host(host):
            # The repo, the bwrap binary and the systemd user bus are all on the
            # OTHER machine. A worktree cut here would guard a path the session
            # never sees, so remote sessions stay exactly as they are until the
            # node agent grows its own guard (see the report's deferred list).
            return ctx

        want_worktree = bool(
            settings.guard_worktree_default if create_worktree is None else create_worktree
        )

        if not settings.guard_enabled:
            # Guard off: the pre-guard behaviour, byte for byte — an explicit
            # request still gets the old timestamp-slug worktree, nothing else
            # changes. This is the escape hatch, so it must stay honest.
            ctx["legacy"] = ctx["will_worktree"] = bool(want_worktree and ctx["explicit"])
            return ctx

        repo_root = (
            await asyncio.to_thread(_git_repo_root, ctx["workspace"])
            if want_worktree else None
        )
        ctx["repo_root"] = repo_root
        ctx["will_worktree"] = want_worktree and (repo_root is not None or ctx["explicit"])
        sandbox_possible = bool(settings.guard_sandbox_enabled)
        if not (ctx["will_worktree"] or sandbox_possible):
            if want_worktree:
                await record_event(
                    self.db, "session:unguarded",
                    f"{ctx['workspace']} is not a git repository — no worktree, no "
                    "rollback point",
                    session_id=session_id, path=ctx["workspace"], severity="warning",
                )
                ctx["degraded"] = "workspace is not a git repository"
            return ctx

        pre = await asyncio.to_thread(preflight)
        ctx["preflight"] = pre
        if not pre["spawn_allowed"]:
            reasons = "; ".join(pre.get("reasons") or ["preflight refused the spawn"])
            await record_event(
                self.db, "spawn:refused", reasons,
                session_id=session_id, path=ctx["workspace"], blocked=True, severity="critical",
            )
            await self._notify_guard(
                "spawn_refused",
                f"Refused a coding session in {ctx['workspace']}: {reasons}",
                ctx["workspace"],
            )
            raise RuntimeError(f"Guard refused this coding session — {reasons}")

        ctx["active"] = True
        return ctx

    async def _guard_provision(self, ctx: dict, session_id: str) -> dict:
        """Cut the worktree planned by `_guard_plan`. The git half.

        Two failure modes, deliberately different:
          - the caller EXPLICITLY asked for a worktree → ValueError, unchanged
            from before the guard (the route turns it into a 400);
          - the sandbox is on and the worktree failed → refuse, because the
            worktree is the only path the sandbox binds read-write and falling
            back would hand the agent Ben's live checkout instead.
        Otherwise degrade to the live checkout with a recorded guard event —
        that is today's behaviour, and it is visible rather than silent.
        """
        if not ctx.get("will_worktree"):
            return ctx
        source = ctx.get("repo_root") or ctx["workspace"]
        try:
            if ctx.get("legacy"):
                worktree_path, worktree_branch, _ = await asyncio.to_thread(
                    _create_worktree, source, ctx.get("worktree_name")
                )
                ctx["source_repo"] = source
                ctx["worktree"] = worktree_path
                ctx["branch"] = worktree_branch
                return ctx
            gs = await get_git_guard(self.db).prepare_session(
                source, session_id, ctx.get("worktree_name")
            )
        except (GuardGitError, WorktreeError) as exc:
            if ctx.get("explicit"):
                raise ValueError(
                    f"Could not provision a worktree at {source!r}: {exc}"
                ) from exc
            if settings.guard_sandbox_enabled:
                await record_event(
                    self.db, "spawn:refused",
                    f"sandbox is enabled but the worktree failed ({exc}) — the live "
                    "checkout would be the only writable path",
                    session_id=session_id, path=source, blocked=True, severity="critical",
                )
                raise RuntimeError(
                    "Guard refused this coding session — the sandbox is enabled but no "
                    f"worktree could be created at {source!r}: {exc}"
                ) from exc
            await record_event(
                self.db, "session:unguarded",
                f"worktree unavailable at {source} ({exc}) — the session runs in the "
                "live checkout",
                session_id=session_id, path=source, severity="warning",
            )
            ctx["degraded"] = f"worktree unavailable: {exc}"
            return ctx

        ctx["source_repo"] = gs["repo"]
        ctx["worktree"] = gs["worktree"]
        ctx["branch"] = gs["branch"]
        ctx["start_tag"] = gs.get("start_tag")
        ctx["mirror"] = gs.get("mirror")
        return ctx

    async def _notify_guard(self, event_type: str, detail: str, workspace: str) -> None:
        """Guard refusals are cockpit/digest material, not a page: the caller
        already got the reason back as a 409, and a low-memory box would
        otherwise page Ben once per attempted spawn."""
        if not self.notification_service:
            return
        try:
            await self.notification_service.notify(
                source="guard",
                event_type=event_type,
                detail=detail,
                cooldown_seconds=300,
                project_path=workspace,
                severity="medium",
                needs_human=False,
            )
        except Exception:  # pragma: no cover - an alert must never block a refusal
            logger.debug("guard notify failed for %s", event_type, exc_info=True)

    def _guard_argv_prefix(self, session_id: str, guard: Optional[dict]) -> list[str]:
        """systemd-run → bwrap → agent. Order matters both ways:

        the transient scope must own the whole process tree (and systemd-run
        from *inside* the sandbox would need the user bus, which the read-only
        /run does not give it), while bwrap must be the thing that execs the
        agent.
        """
        if not (guard and guard.get("active") and settings.guard_enabled):
            return []
        pre = guard.get("preflight") or {}
        argv: list[str] = []
        if pre.get("systemd_run_present"):
            argv += resource_prefix(session_id)
        if guard.get("sandbox"):
            argv += build_sandbox_prefix(
                guard.get("worktree") or guard.get("workspace") or os.getcwd(),
                session_id,
                source_repo=guard.get("source_repo"),
            )
        return argv

    def _guard_env(self, session_id: str, command_env: Optional[dict]) -> dict:
        """The full environment a guarded session gets: scrubbed, plus whatever
        the backend asked for (ARIA_MANAGED, PI_OFFLINE, …)."""
        env = session_env(os.environ, session_id=session_id, create_tmp=True)
        env.update(command_env or {})
        return env

    def _guard_env_argv(self, session_id: str, command_env: Optional[dict]) -> list[str]:
        """`env -u SECRET … KEY=VALUE … ` — the same scrub, as an argv prefix.

        The shell substrate hands tmux a `bash -lc` string, and a login shell
        re-sources Ben's profile AFTER we build that string; exporting
        replacements up front would be undone by anything the profile exports.
        `env -u` inside the command runs last, so it wins.

        The removals are derived from `session_env()`'s own output rather than
        from a copy of its denylist — a secret added there is scrubbed here with
        no second edit. The known gap: this can only unset names aria-api itself
        has, so a credential exported *only* by the login shell survives (see
        the INTEGRATION SPEC's `sandbox.sensitive_env_names()` request). The
        file masks, not this, are the real defence for credentials.
        """
        base = dict(os.environ)
        scrubbed = self._guard_env(session_id, command_env)
        argv = ["env"]
        for name in sorted(set(base) - set(scrubbed)):
            argv += ["-u", name]
        for key in sorted(scrubbed):
            if base.get(key) != scrubbed[key]:
                argv.append(f"{key}={scrubbed[key]}")
        return argv

    async def checkpoint_session(self, session_id: str, reason: str = "interval") -> dict:
        """Commit the session's work — ARIA holds the git pen, not the agent.

        Safe to call blindly: a clean tree is a no-op, an unguarded session is a
        no-op, and nothing here raises into a caller (the watchdog nudges and
        the kill paths all call it on their way through).
        """
        if not (settings.guard_enabled and settings.guard_checkpoint_enabled):
            return {"ok": False, "committed": False, "reason": "guard checkpoints disabled"}
        try:
            result = await get_git_guard(self.db).checkpoint(session_id, reason=reason)
        except Exception as exc:  # noqa: BLE001 — see docstring
            logger.warning("guard checkpoint failed for %s: %s", session_id, exc)
            return {"ok": False, "committed": False, "reason": str(exc)}

        if result.get("committed"):
            # Make `session_checkpoints` real (§7.2): the crash-recovery record
            # has existed for months as metadata with no commit behind it, and
            # resume_session reads `last_commit` from it. Now there is one.
            session = await self.get_session(session_id)
            if session:
                try:
                    await write_checkpoint(
                        self.db, session_id, session["workspace"],
                        notes=f"guard checkpoint ({reason}) {str(result.get('sha'))[:12]}",
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("metadata checkpoint failed for %s: %s", session_id, exc)
        return result

    async def _checkpoint_loop(self, session_id: str) -> None:
        """Periodic checkpoints for a running session. The interval exists so
        that "how much work can one bad session lose" has a bounded answer;
        `guard_checkpoint_interval_seconds` is that bound."""
        interval = int(settings.guard_checkpoint_interval_seconds or 0)
        if interval <= 0:
            return
        try:
            while True:
                await asyncio.sleep(interval)
                session = await self.get_session(session_id)
                if not session or session.get("status") not in ("running", "queued"):
                    return
                await self.checkpoint_session(session_id, reason="interval")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("guard checkpoint loop stopped for %s: %s", session_id, exc)

    def _start_checkpoint_loop(self, session_id: str, guard: Optional[dict]) -> None:
        if not (guard and guard.get("worktree") and settings.guard_enabled
                and settings.guard_checkpoint_enabled):
            return
        if session_id in self._checkpoint_tasks:
            return
        self._checkpoint_tasks[session_id] = asyncio.create_task(
            self._checkpoint_loop(session_id)
        )

    def _stop_checkpoint_loop(self, session_id: str) -> None:
        task = self._checkpoint_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def shutdown(self) -> None:
        """Cancel the per-session checkpoint loops on service shutdown.

        The sessions themselves are deliberately left alone: aria-api restarts
        routinely (a deploy, a config change) and a restart that killed every
        coding agent would be its own outage. Only the timers stop; a restarted
        service picks the sessions back up through the watchdog, and the last
        checkpoint before the restart is already committed.
        """
        for session_id in list(self._checkpoint_tasks):
            self._stop_checkpoint_loop(session_id)

    async def stop_all_running(self, reason: str = "stop_all") -> dict:
        """Stop every running/queued coding session — the half the killswitch
        and the e-stop were missing (proposal §7.3: they blocked new spawns
        while a rogue agent kept running).

        Checkpoint first, then kill: a stop that loses the work in flight makes
        the stop button expensive to press, and an expensive stop button does
        not get pressed. Never raises — a stop path that can fail is not a stop
        path.
        """
        stopped: list[str] = []
        failed: list[dict] = []
        try:
            sessions = list(await self.db.coding_sessions.find(
                {"status": {"$in": ["running", "queued"]}}
            ).to_list(length=200) or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("stop_all_running could not list sessions: %s", exc)
            return {"stopped": 0, "sessions": [], "failed": [], "error": str(exc)}

        for session in sessions:
            session_id = str(session.get("_id"))
            try:
                # stop_session checkpoints a guarded session on its way out, so
                # the work in flight is committed before anything is killed.
                if await self.stop_session(session_id):
                    stopped.append(session_id)
            except Exception as exc:  # noqa: BLE001 - one stuck session must not
                # block the rest; this runs while something is already wrong.
                logger.warning("stop_all_running: %s did not stop: %s", session_id, exc)
                failed.append({"session_id": session_id, "error": str(exc)})
        if stopped or failed:
            await record_event(
                self.db, "sessions:stopped",
                f"{reason}: stopped {len(stopped)} session(s), {len(failed)} failed",
                blocked=True, severity="warning", actor=reason,
            )
        logger.warning(
            "stop_all_running(%s): stopped %d session(s), %d failed",
            reason, len(stopped), len(failed),
        )
        return {"stopped": len(stopped), "sessions": stopped, "failed": failed}

    async def _launch_substrate(
        self, session_id, command, backend_name, workspace_path, visible, host, prompt,
        guard=None,
    ) -> dict:
        """Launch the chosen substrate for an already-persisted (and slot-holding)
        session doc, install its watch task, and return the running doc. Assumes a
        concurrency slot is held; the watch task releases it on finalize."""
        # Remote host: run the session on that machine's aria-node. The node
        # creates a claude-coding-* tmux shell locally (auto-captured back into
        # the fleet); we drive it via the host-aware ShellService dispatch, so
        # the watchdog/checkpoint/review overlay + Ralph loop work unchanged.
        from aria.nodes import is_remote_host
        if is_remote_host(host):
            return await self._start_remote_shell_session(
                session_id, host, command, workspace_path, backend_name
            )

        # Everything below runs on THIS box, so everything below is guarded.
        # `prefix` is empty and `guarded` False whenever the guard does not
        # govern this session, which keeps every pre-guard path byte-identical.
        guarded = bool(guard and guard.get("active") and settings.guard_enabled)
        prefix = self._guard_argv_prefix(session_id, guard)

        # If visible mode requested and tmux is available, spawn in a tmux pane
        if visible and self.tmux_manager:
            visible_argv = [*prefix, *command.argv]
            if guarded:
                visible_argv = self._guard_env_argv(session_id, command.env) + visible_argv
            shell_cmd = " ".join(visible_argv)
            if command.cwd:
                shell_cmd = f"cd {command.cwd} && {shell_cmd}"
            title = f"[{backend_name}] {prompt[:40]}"
            pane = await self.tmux_manager.spawn_pane(session_id, shell_cmd, title=title)
            await self.db.coding_sessions.update_one(
                {"_id": session_id},
                {"$set": {
                    "status": "running",
                    "tmux_pane_id": pane.pane_id,
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
            self._start_checkpoint_loop(session_id, guard)
            logger.info("Started visible tmux session %s (pane %s)", session_id, pane.pane_id)
            return await self.get_session(session_id)

        # Shell substrate: run the coding agent as a watched shell so it joins
        # the fleet (auto-adopted + captured to shell_events) and is drivable via
        # the same observe/drive tools. The watchdog/checkpoint/review overlay
        # still manages it through this manager's interface.
        if self._use_shell_substrate():
            # Launch the agent INTERACTIVELY on the shell so it stays alive and
            # is drivable like any watched shell (send input / observe / stop),
            # rather than -p print mode which runs once and exits. The prompt is
            # kept as the seed positional.
            argv = [*prefix, *(a for a in command.argv if a not in ("-p", "--print"))]
            argv_str = " ".join(shlex.quote(a) for a in argv)
            if guarded:
                # One `env` call carries both the scrub and the backend's own
                # variables, so the credential removals cannot be reordered
                # behind the assignments.
                env_prefix = " ".join(
                    shlex.quote(a) for a in self._guard_env_argv(session_id, command.env)
                )
            else:
                env_prefix = " ".join(
                    f"{k}={shlex.quote(v)}" for k, v in (command.env or {}).items()
                )
            inner = (env_prefix + " " + argv_str).strip()
            launch = "bash -lc " + shlex.quote(inner)
            shell_name = None
            try:
                shell = await self.shell_service.create_shell(
                    name=f"coding-{session_id[:8]}",
                    workdir=command.cwd or workspace_path,
                    launch_command=launch,
                )
                shell_name = shell.name
            except Exception as exc:  # fall back to subprocess substrate
                logger.warning(
                    "shell-substrate spawn failed for %s (%s); using subprocess",
                    session_id, exc,
                )
            if shell_name:
                await self.db.coding_sessions.update_one(
                    {"_id": session_id},
                    {"$set": {
                        "status": "running",
                        "shell_name": shell_name,
                        "updated_at": datetime.now(timezone.utc),
                    }},
                )
                self._watch_tasks[session_id] = asyncio.create_task(
                    self._watch_shell_session(session_id)
                )
                self._start_checkpoint_loop(session_id, guard)
                logger.info("Started coding session %s on shell %s", session_id, shell_name)
                return await self.get_session(session_id)

        if guarded:
            # The subprocess substrate replaces the environment wholesale
            # (`env=command.env or None`), so a guarded session gets the full
            # scrubbed one — PATH and HOME included, which the bare
            # {"ARIA_MANAGED": "1"} it used to get did not have.
            command = CommandSpec(
                argv=[*prefix, *command.argv],
                env=self._guard_env(session_id, command.env),
                cwd=command.cwd,
            )
        running = await self.process_manager.spawn(session_id, command)
        await self.db.coding_sessions.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "status": "running",
                    "pid": running.process.pid,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        self._watch_tasks[session_id] = asyncio.create_task(self._watch_session(session_id))
        self._start_checkpoint_loop(session_id, guard)
        return await self.get_session(session_id)

    async def _start_remote_shell_session(
        self,
        session_id: str,
        node_id: str,
        command,
        workspace_path: str,
        backend_name: str,
    ) -> dict:
        """Start a coding session on a remote node: enqueue a start_session
        command (the node creates the claude-coding-* tmux shell locally), then
        drive it via the host-aware ShellService dispatch."""
        if not self.shell_service:
            raise RuntimeError("shells disabled — cannot start a remote coding session")
        from aria.nodes import commands as node_commands

        argv = [a for a in command.argv if a not in ("-p", "--print")]
        # The backend built argv[0] with THIS host's absolute binary path (e.g.
        # /home/ben/.local/bin/claude). On a remote node the same tool lives at a
        # different path ($HOME differs: /Users/ben vs /home/ben), so use the
        # basename and resolve it via the node's PATH (prepended below).
        if argv and "/" in argv[0]:
            argv[0] = os.path.basename(argv[0])
        argv_str = " ".join(shlex.quote(a) for a in argv)
        env_prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in (command.env or {}).items()
        )
        inner = (env_prefix + " " + argv_str).strip()
        # Ensure user-local bin dirs (e.g. ~/.local/bin, where `claude` lives on
        # macOS) are on PATH. A `bash -l` login shell rebuilds PATH via
        # path_helper — which drops ~/.local/bin — so prepend it INSIDE the
        # command, which runs after profile sourcing. Otherwise the agent binary
        # isn't found and the session exits immediately. Harmless where it's
        # already on PATH.
        inner = 'export PATH="$HOME/.local/bin:$PATH"; ' + inner
        launch = "bash -lc " + shlex.quote(inner)
        shell_name = f"{settings.shells_tmux_session_prefix}coding-{session_id[:8]}"
        workdir = command.cwd or workspace_path

        cmd_id = await node_commands.enqueue_command(
            self.db, node_id, "start_session",
            {
                "shell_name": shell_name,
                "launch": launch,
                "workdir": workdir,
                "backend": backend_name,
                "binary": argv[0] if argv else "",
            },
            idempotency_key=f"coding-start:{node_id}:{session_id}",
        )
        result = await node_commands.await_result(
            self.db, cmd_id, timeout_seconds=settings.node_command_timeout_seconds
        )
        if not result or result.get("status") != "done":
            now = datetime.now(timezone.utc)
            await self.db.coding_sessions.update_one(
                {"_id": session_id},
                {"$set": {
                    "status": "failed",
                    "error": f"node {node_id} unreachable",
                    "updated_at": now,
                    "completed_at": now,
                }},
            )
            raise RuntimeError(f"coding node {node_id} unreachable — session not started")

        payload = result.get("result") or {}
        backend_error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(backend_error, dict) and backend_error.get("code") == "coding_backend_unavailable":
            now = datetime.now(timezone.utc)
            await self.db.coding_sessions.update_one(
                {"_id": session_id},
                {"$set": {
                    "status": "failed",
                    "error": backend_error.get("reason") or "backend unavailable",
                    "updated_at": now,
                    "completed_at": now,
                }},
            )
            raise CodingBackendUnavailableError(
                str(backend_error.get("backend") or backend_name),
                str(backend_error.get("reason") or "backend unavailable"),
                retryable=bool(backend_error.get("retryable")),
            )

        # Pre-register the shell doc (host=node_id) so it's drivable immediately,
        # before the node's capture loop first pushes events for it.
        await self.shell_service.register_shell(shell_name, project_dir=workdir, host=node_id)
        now = datetime.now(timezone.utc)
        await self.db.coding_sessions.update_one(
            {"_id": session_id},
            {"$set": {"status": "running", "shell_name": shell_name, "updated_at": now}},
        )
        self._watch_tasks[session_id] = asyncio.create_task(
            self._watch_shell_session(session_id)
        )
        logger.info(
            "Started remote coding session %s on node %s (shell %s)",
            session_id, node_id, shell_name,
        )
        return await self.get_session(session_id)

    async def stop_session(self, session_id: str) -> bool:
        session = await self.get_session(session_id)
        if not session:
            return False

        # Last checkpoint before the process dies: whatever the agent had
        # written but not committed is the work a stop would otherwise throw
        # away, and the guard is the only thing that commits it.
        self._stop_checkpoint_loop(session_id)
        if (session.get("guard") or {}).get("worktree"):
            await self.checkpoint_session(session_id, reason="stop")

        # Handle shell-substrate sessions (kill the watched tmux shell) --
        # this covers pi-code too now that it runs on the shell substrate.
        if session.get("shell_name") and self.shell_service:
            try:
                await self.shell_service.kill_shell(session["shell_name"])
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("stop: kill_shell failed for %s: %s", session_id, exc)
        # Handle tmux-visible sessions
        elif session.get("tmux_pane_id"):
            if self.tmux_manager:
                await self.tmux_manager.kill_pane(session_id)
            # If tmux_manager is None but session was tmux-based, still mark as stopped
        elif session.get("status") == "queued":
            # A queued session has no process to kill yet — cancelling its
            # deferred launcher below IS the stop. Without this branch it fell
            # through to process_manager.stop(), returned False, and stayed
            # `queued`, so a killswitch sweep reported "stopped 0" while three
            # sessions sat waiting for a slot.
            pass
        else:
            stopped = await self.process_manager.stop(session_id)
            if not stopped:
                return False
        await self.db.coding_sessions.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "status": "stopped",
                    "updated_at": datetime.now(timezone.utc),
                    "completed_at": datetime.now(timezone.utc),
                    "exit_code": None,
                }
            },
        )
        watch_task = self._watch_tasks.pop(session_id, None)
        if watch_task is not None:
            watch_task.cancel()
        # Free the concurrency slot promptly (idempotent — the watch task's
        # finally would also release it once the cancellation lands).
        await self._release_slot(session_id)
        if self.notification_service:
            try:
                await self.notification_service.notify(
                    source=f"coding:{session_id}",
                    event_type="stopped",
                    detail=f"Stopped coding session in {session['workspace'] if session else 'unknown workspace'}",
                    cooldown_seconds=10,
                )
            except Exception:
                pass
        return True

    async def get_session(self, session_id: str) -> Optional[dict]:
        return await self.db.coding_sessions.find_one({"_id": session_id})

    async def delete_session(self, session_id: str) -> bool:
        """Permanently remove a coding_sessions record — for cleaning up
        finished/stale history, not a substitute for stop_session. Refuses a
        session still marked "running"/"queued" so a caller doesn't delete
        live state out from under an active process; call stop_session first
        (or wait for it to finish) if that's really the intent. Does not
        touch the underlying shell/shell_events — those are owned by the
        watched-shell subsystem's own retention (shells/prune.py), not this
        manager, and a deleted coding_sessions row shouldn't silently take a
        shell's still-useful scrollback with it.
        """
        session = await self.get_session(session_id)
        if not session:
            return False
        if session.get("status") in ("running", "queued"):
            raise ValueError(
                f"Refusing to delete session {session_id} while status={session['status']!r} "
                "-- stop it first."
            )
        result = await self.db.coding_sessions.delete_one({"_id": session_id})
        return result.deleted_count > 0

    async def wait_for_session(
        self, session_id: str, timeout: Optional[float] = None, poll_interval: float = 1.0
    ) -> Optional[dict]:
        """Block until a session reaches a terminal state, then return its doc
        with a `result_summary` attached (from the TASK_DONE/ERROR mail the
        finalize paths emit). This is the join primitive workflow fan-out builds
        on. Poll-based, so it is restart-safe (survives loss of the in-memory
        watch task) and handles the queued -> running -> done progression
        uniformly. On timeout returns the current (non-terminal) doc with
        `timed_out=True`; returns None if the session doesn't exist."""
        terminal = {"completed", "failed", "stopped"}
        start = datetime.now(timezone.utc)
        while True:
            session = await self.get_session(session_id)
            if not session:
                return None
            if session.get("status") in terminal:
                break
            if timeout is not None and (
                datetime.now(timezone.utc) - start
            ).total_seconds() >= timeout:
                return {**session, "result_summary": None, "timed_out": True}
            await asyncio.sleep(poll_interval)

        result_summary = None
        try:
            mail = await self.mailbox.get_session_mail(session_id)
            for m in reversed(mail):
                if m.msg_type in (
                    MessageType.TASK_DONE, MessageType.ERROR, MessageType.RESULT,
                ):
                    result_summary = m.body
                    break
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("wait_for_session mail lookup failed for %s: %s", session_id, exc)
        return {**session, "result_summary": result_summary}

    async def list_sessions(self, status: Optional[str] = None) -> list[dict]:
        query = {"status": status} if status else {}
        return await self.db.coding_sessions.find(query).sort("created_at", -1).to_list(length=200)

    async def get_output(self, session_id: str, lines: int = 50) -> str:
        session = await self.get_session(session_id)
        # Shell-substrate sessions (pi-code included, now that it runs on the
        # shell substrate too): live ANSI-stripped pane from the fleet; if the
        # tmux session has ended (completed), fall back to captured scrollback.
        if session and session.get("shell_name") and self.shell_service:
            screen = await self.shell_service.current_screen(session["shell_name"], lines=lines)
            if screen:
                return screen
            events = await self.shell_service.list_events(
                session["shell_name"], limit=lines, kinds=["output"], sort=1
            )
            return "\n".join(e.text_clean for e in events[-lines:])
        # Try tmux capture for visible (aria-agents pane) sessions
        if session and session.get("tmux_pane_id") and self.tmux_manager:
            return await self.tmux_manager.capture_output(session_id, lines=lines)
        return self.process_manager.get_output(session_id, lines=lines)

    async def send_input(self, session_id: str, text: str) -> bool:
        session = await self.get_session(session_id)
        # Shell-substrate sessions (including the real Pi TUI): input is plain
        # tmux send-keys, exactly like Claude Code/Codex. The process is already
        # running continuously, so start_session and Ralph-loop nudges are the
        # places where new-work safety gates apply.
        if session and session.get("shell_name") and self.shell_service:
            try:
                line, _screen = await self.shell_service.send_input(session["shell_name"], text)
                return line > 0
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("send_input(shell) failed for %s: %s", session_id, exc)
                return False
        return await self.process_manager.send_input(session_id, text)

    async def get_diff(self, session_id: str) -> str:
        session = await self.get_session(session_id)
        if not session:
            return ""
        args = ["git", "-C", session["workspace"], "diff", "--no-ext-diff"]
        start_tag = (session.get("guard") or {}).get("start_tag")
        if start_tag:
            # A guarded session's work is COMMITTED as it goes (checkpoints
            # every few minutes), so a bare `git diff` reports an empty tree
            # for a session that has changed 40 files. Diffing from the
            # pre-session tag gives the whole session's work, committed and
            # uncommitted — which is what every caller of this actually wants
            # (review, the cockpit, the stuck-detector's no-diff signal).
            args.append(start_tag)
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return "[git diff timed out after 30s]"
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        return output or error

    async def resume_session(
        self,
        workspace: str,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        conversation_id: Optional[str] = None,
        visible: bool = False,
    ) -> Optional[dict]:
        """Resume a crashed session from its checkpoint.

        Looks for the most recent checkpoint for the given workspace,
        builds a resume prompt with checkpoint context, and starts a
        new session that continues the original work.

        Returns the new session dict, or None if no checkpoint found.
        """
        checkpoint = await find_resumable_checkpoint(self.db, workspace)
        if checkpoint is None:
            return None

        # Load the original session to get the prompt
        original = await self.get_session(checkpoint.session_id)
        if not original:
            # Original session doc deleted — use checkpoint data to resume
            original = {
                "prompt": checkpoint.notes or "Continue previous work",
                "backend": None,
                "model": None,
                "conversation_id": None,
            }

        original_prompt = original.get("prompt", "")
        resume_prompt = build_resume_prompt(checkpoint, original_prompt)

        logger.info(
            "Resuming session from checkpoint %s (branch=%s, commit=%s)",
            checkpoint.session_id, checkpoint.branch, checkpoint.last_commit,
        )

        return await self.start_session(
            workspace=workspace,
            backend=backend or original.get("backend"),
            prompt=resume_prompt,
            branch=checkpoint.branch,
            model=model or original.get("model"),
            conversation_id=conversation_id or original.get("conversation_id"),
            visible=visible,
        )

    async def _watch_session(self, session_id: str) -> None:
        try:
            exit_code = await self.process_manager.wait(session_id)
            if exit_code is None:
                return

            session = await self.get_session(session_id)
            if session and session.get("status") == "stopped":
                return

            # The agent has exited; anything it wrote and never committed is
            # still sitting in the worktree. Commit it before finalizing —
            # "the session finished" must not mean "the diff is gone".
            if ((session or {}).get("guard") or {}).get("worktree"):
                await self.checkpoint_session(session_id, reason="exit")

            # Some backends have exit codes that are a real result, not a
            # crash (e.g. pool's exit 4 -- "task ran but couldn't complete
            # it", see backends/pool.py). Skip the crash-recovery checkpoint
            # for those; it exists for actual process crashes.
            expected_failure = False
            if session and exit_code != 0:
                try:
                    backend = self.registry.get(session.get("backend"))
                    is_expected = getattr(backend, "is_expected_failure_exit_code", None)
                    if is_expected:
                        expected_failure = bool(is_expected(exit_code))
                except Exception:
                    expected_failure = False

            # Write checkpoint before marking final status (crash recovery)
            if session and exit_code != 0 and not expected_failure:
                try:
                    await write_checkpoint(
                        self.db,
                        session_id,
                        session["workspace"],
                        notes=f"Session exited with code {exit_code}",
                    )
                except Exception as e:
                    logger.warning("Failed to write crash checkpoint: %s", e)

            await self.db.coding_sessions.update_one(
                {"_id": session_id},
                {
                    "$set": {
                        "status": "completed" if exit_code == 0 else "failed",
                        "updated_at": datetime.now(timezone.utc),
                        "completed_at": datetime.now(timezone.utc),
                        "exit_code": exit_code,
                    }
                },
            )
            if self.notification_service:
                try:
                    await self.notification_service.notify(
                        source=f"coding:{session_id}",
                        event_type="completed" if exit_code == 0 else "error",
                        detail=f"Session exited with code {exit_code}",
                        cooldown_seconds=10,
                    )
                except Exception:
                    pass

            # Send inter-agent mail notification
            session = await self.get_session(session_id)
            try:
                status = "completed" if exit_code == 0 else "failed"
                output_tail = self.process_manager.get_output(session_id, lines=10)
                await self.mailbox.send_task_done(
                    sender=f"coding:{session.get('backend', 'unknown')}",
                    recipient="orchestrator",
                    session_id=session_id,
                    result_summary=output_tail or f"Session {status}",
                    exit_status=status,
                    conversation_id=session.get("conversation_id") if session else None,
                )
            except Exception as e:
                logger.debug("Failed to send task_done mail: %s", e)

            if session and session.get("conversation_id"):
                summary = f"Coding session {session_id} finished with status {'completed' if exit_code == 0 else 'failed'}."
                try:
                    conv_oid = ObjectId(session["conversation_id"])
                except Exception:
                    return
                await self.db.conversations.update_one(
                    {"_id": conv_oid},
                    {
                        "$push": {
                            "messages": {
                                "id": str(uuid4()),
                                "role": "assistant",
                                "content": summary,
                                "created_at": datetime.now(timezone.utc),
                                "memory_processed": False,
                            }
                        },
                        "$set": {"updated_at": datetime.now(timezone.utc)},
                        "$inc": {"stats.message_count": 1},
                    },
                )
        except asyncio.CancelledError:
            return
        finally:
            self._watch_tasks.pop(session_id, None)
            self._stop_checkpoint_loop(session_id)
            await self._release_slot(session_id)

    async def _watch_shell_session(self, session_id: str) -> None:
        """Watch a shell-substrate coding session: poll until its tmux session
        ends (the agent exited), then finalize. The watchdog/checkpoint/review
        overlay manages it in the meantime via this manager's interface, exactly
        as for a subprocess session."""
        try:
            session = await self.get_session(session_id)
            shell_name = (session or {}).get("shell_name")
            if not (shell_name and self.shell_service):
                return
            interval = max(2, settings.coding_watchdog_interval_seconds)
            while True:
                await asyncio.sleep(interval)
                # Host-aware liveness: local → tmux has_session; remote → node
                # online and the shell not marked stopped.
                if not await self.shell_service.session_alive(shell_name):
                    break  # session gone -> agent finished
                cur = await self.get_session(session_id)
                if cur and cur.get("status") in ("stopped", "completed", "failed"):
                    return  # finalized elsewhere (e.g. stop_session)

            cur = await self.get_session(session_id)
            if cur and cur.get("status") == "stopped":
                return
            # Same rule as the subprocess watcher: commit what the agent left
            # behind before the session is marked finished.
            if ((cur or {}).get("guard") or {}).get("worktree"):
                await self.checkpoint_session(session_id, reason="exit")
            output_tail = await self.get_output(session_id, lines=10)
            now = datetime.now(timezone.utc)
            await self.db.coding_sessions.update_one(
                {"_id": session_id},
                {"$set": {
                    "status": "completed",
                    "updated_at": now,
                    "completed_at": now,
                    "exit_code": None,
                }},
            )
            # Mark the watched-shell row stopped so a finished sub-agent doesn't
            # linger in the fleet (its tmux session is already gone).
            try:
                await self.shell_service.mark_stopped(shell_name)
            except Exception:
                pass
            if self.notification_service:
                try:
                    await self.notification_service.notify(
                        source=f"coding:{session_id}",
                        event_type="completed",
                        detail="Coding session finished",
                        cooldown_seconds=10,
                    )
                except Exception:
                    pass
            try:
                await self.mailbox.send_task_done(
                    sender=f"coding:{(cur or {}).get('backend', 'unknown')}",
                    recipient="orchestrator",
                    session_id=session_id,
                    result_summary=output_tail or "Session completed",
                    exit_status="completed",
                    conversation_id=(cur or {}).get("conversation_id"),
                )
            except Exception as e:
                logger.debug("Failed to send task_done mail: %s", e)
        except asyncio.CancelledError:
            return
        finally:
            self._watch_tasks.pop(session_id, None)
            self._stop_checkpoint_loop(session_id)
            await self._release_slot(session_id)
