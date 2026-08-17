"""
ARIA - Main FastAPI Application

Phase: 1, 3
Purpose: FastAPI application entry point

Related Spec Sections:
- Section 5: API Specification
- Section 7: Project Structure
"""

import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from aria.config import settings
from aria.core.logging import setup_logging

# Initialize structured logging with secret scrubbing before anything else
setup_logging(json_output=not settings.debug, level="DEBUG" if settings.debug else "INFO")
from aria.db.migrations import run_migrations
from aria.db.mongodb import connect_db, close_db, get_database
from aria.api.routes import admin, capabilities, health, conversations, agents, memories, memory_api, tools, tts, stt, usage, signal, notifications, tasks, research, coding_sessions, routing, infrastructure, workflows, schedules, killswitch, skills, groupchat, autopilot, heartbeat, dreams, awareness, shells, planning, alerts, nodes, shared, digest, shell_nudge, obsidian, linear, benchmarks, llm_proxy, ontology, guard, steward, improve
from aria.api.deps import (
    get_audit_service,
    get_coding_session_manager,
    get_killswitch,
    get_mcp_manager,
    get_notification_service,
    get_orchestrator,
    get_rate_limiter,
    get_signal_service,
    get_skill_registry,
    get_task_runner,
    get_tool_router,
    resolve_coding_watchdog,
    resolve_rate_limit_watchdog,
    resolve_escalation_manager,
    resolve_awareness_service,
    resolve_dream_service,
    resolve_heartbeat_service,
    resolve_scheduler,
    resolve_shell_service,
)
from aria.core.claude_runner import ClaudeRunner
from aria.core.soul import soul_manager
from aria.tools.builtin import (
    ClaudeAgentTool,
    DeepThinkTool,
    DocumentGenerationTool,
    FilesystemTool,
    GetCodingDiffTool,
    GetCodingOutputTool,
    ListCodingSessionsTool,
    PiCodingAgentTool,
    ScreenshotTool,
    SearchAgentTool,
    SendToCodingSessionTool,
    ShellTool,
    SoulTool,
    BrowsePageTool,
    RuntimeUpdateCheckTool,
    StartCodingSessionTool,
    StopCodingSessionTool,
    WebTool,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown events."""
    # Startup
    import logging as _logging
    startup_logger = _logging.getLogger("aria.startup")

    soul_manager.ensure_file()
    await connect_db()
    await run_migrations(await get_database())

    # Validate critical services at startup
    import httpx
    db = await get_database()

    # --- Guard: policy tamper check (steward plan §7.4, principle 12) -------
    # Every documented self-improvement failure has the same shape: the agent
    # edits the thing that judges or stops it (DGM removing its hallucination
    # markers, AI Scientist raising its own timeout, o3 rewriting a shutdown
    # script). So the enforced guard policy is hashed on first run and verified
    # on every boot; a mismatch is an e-stop, not a log line, because the only
    # innocent explanation is a deliberate change that should have been blessed.
    if settings.guard_enabled:
        from aria.guard.policy import verify_policy

        try:
            _guard_verdict = await verify_policy(db)
            if not _guard_verdict.get("ok"):
                startup_logger.critical(
                    "GUARD POLICY %s: %s", _guard_verdict.get("status"), _guard_verdict
                )
                from aria.api.deps import resolve_estop_manager

                # auto_thaw=False: a rate-limit freeze may lift itself, but a
                # policy that no longer matches its blessed hash must be looked
                # at by a human before agents run again.
                await (await resolve_estop_manager(db)).activate(
                    reason=(
                        f"guard policy {_guard_verdict.get('status')} "
                        f"({_guard_verdict.get('path')})"
                    ),
                    triggered_by="guard",
                    auto_thaw=False,
                )
            else:
                startup_logger.info(
                    "Guard policy %s (%s)",
                    _guard_verdict.get("status"),
                    _guard_verdict.get("source"),
                )
        except Exception:
            startup_logger.exception("Guard policy verification failed at startup")

    # Check embedding service. The persisted capability switch is loaded later
    # in this lifespan (it needs the migrations to have run), so this early
    # probe consults the *live* switch and falls back to the config default —
    # either way, probing a service ARIA has been told not to use would only
    # produce a startup warning nobody should act on.
    from aria.memory.capabilities import COLLECTION as _CAPS_COLLECTION, DOC_ID as _CAPS_ID

    _embeddings_wanted = settings.embeddings_enabled
    try:
        _persisted = await db[_CAPS_COLLECTION].find_one({"_id": _CAPS_ID})
        if _persisted and "enabled" in (_persisted.get("embeddings") or {}):
            _embeddings_wanted = bool(_persisted["embeddings"]["enabled"])
    except Exception:  # noqa: BLE001 — the real load happens below
        pass

    if not _embeddings_wanted:
        startup_logger.info(
            "Embedding service: SKIPPED — embeddings capability is switched off "
            "(memories store as embedding_pending; re-enable to backfill)"
        )
    else:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{settings.embedding_url.rstrip('/').replace('/v1', '')}/health"
                )
                if resp.status_code == 200:
                    startup_logger.info("Embedding service: connected")
                else:
                    startup_logger.warning("Embedding service: returned HTTP %d (degraded mode)", resp.status_code)
        except Exception as e:
            startup_logger.warning("Embedding service: unreachable (%s) — memories will be stored without embeddings", e)

    # Check LLM backends
    from aria.llm.manager import llm_manager
    available_backends = []
    for backend_name in ("llamacpp", "context1", "anthropic", "openai", "openrouter"):
        avail, reason = llm_manager.is_backend_available(backend_name)
        if avail:
            available_backends.append(backend_name)
    if available_backends:
        startup_logger.info("LLM backends available: %s", ", ".join(available_backends))
    else:
        startup_logger.warning("No LLM backends configured — ARIA will not be able to generate responses")

    # Initialize built-in tools
    tool_router = get_tool_router()
    db = await get_database()
    audit_service = await get_audit_service(db)
    coding_manager = await get_coding_session_manager(db)
    task_runner = await get_task_runner(db)
    tool_router.set_audit_hook(audit_service.log_event)
    tool_router.set_db(db)
    tool_router.register_tool(FilesystemTool())
    tool_router.register_tool(GetCodingDiffTool(coding_manager))
    tool_router.register_tool(GetCodingOutputTool(coding_manager))
    tool_router.register_tool(ListCodingSessionsTool(coding_manager))
    tool_router.register_tool(SendToCodingSessionTool(coding_manager))
    tool_router.register_tool(
        ShellTool(
            allowed_commands=settings.shell_allowed_commands,
            denied_commands=settings.shell_denied_commands,
            working_directory=settings.coding_default_workspace,
            sandbox_enabled=settings.shell_sandbox_enabled,
        )
    )
    tool_router.register_tool(StartCodingSessionTool(coding_manager))
    tool_router.register_tool(StopCodingSessionTool(coding_manager))
    tool_router.register_tool(WebTool())
    tool_router.register_tool(BrowsePageTool())
    tool_router.register_tool(RuntimeUpdateCheckTool())
    tool_router.register_tool(ScreenshotTool())
    tool_router.register_tool(DocumentGenerationTool())
    tool_router.register_tool(SoulTool())
    if ClaudeRunner.is_available():
        tool_router.register_tool(ClaudeAgentTool())
        tool_router.register_tool(DeepThinkTool())
        startup_logger.info("Claude Agent + Deep Think tools registered (CLI available)")

    # Compatibility delegation tool backed by the real external Pi CLI.
    tool_router.register_tool(PiCodingAgentTool(coding_manager))
    startup_logger.info("Pi Coding Agent shell tool registered")

    # Search Agent — context-1 agentic retrieval over memory/web/files
    ctx1_available, _ = llm_manager.is_backend_available("context1")
    if ctx1_available:
        tool_router.register_tool(SearchAgentTool(db))
        startup_logger.info("Search Agent tool registered (context-1)")

    # Restore persisted MCP servers
    mcp_manager = get_mcp_manager()
    restored = await mcp_manager.load_saved_servers(db)
    if restored:
        for tool in mcp_manager.get_all_tools():
            try:
                tool_router.register_tool(tool)
            except ValueError:
                pass

    watchdog = await resolve_coding_watchdog(db, coding_manager)
    await watchdog.start()

    # Automated emergency stop: watches cloud-backend circuit breakers for
    # rate limiting and freezes/thaws the global estop accordingly. It also
    # raises/resolves a CRITICAL escalation on each rate-limit episode.
    rate_limit_watchdog = await resolve_rate_limit_watchdog(db)
    await rate_limit_watchdog.start()

    # Escalation protocol: periodically re-escalate stale open escalations.
    escalation_manager = await resolve_escalation_manager(db)
    await escalation_manager.start()

    await task_runner.recover_stale_tasks()

    scheduler = await resolve_scheduler(db, task_runner)
    await scheduler.start()

    # Load killswitch state
    ks = get_killswitch()
    await ks.load_state(db)

    # Retrieval capability switches (mongot / embeddings). Loaded from the
    # persisted doc, NOT reset to the config defaults — a capability an
    # operator switched off must stay off across a restart, or the alerts they
    # silenced come straight back. See memory/capabilities.py.
    from aria.memory.capabilities import retrieval_capabilities
    await retrieval_capabilities.load_state(db)

    if settings.embedding_backfill_enabled:
        from aria.memory.backfill import EmbeddingBackfillWorker
        embedding_backfill = EmbeddingBackfillWorker(db)
        await embedding_backfill.start()
        app.state.embedding_backfill = embedding_backfill
        # This is what makes re-enabling self-healing: flipping the embeddings
        # switch back on drains the embedding_pending backlog immediately,
        # rather than waiting out the worker's interval.
        retrieval_capabilities.set_backfill_trigger(embedding_backfill.kick)

    # Load installed skills
    skill_registry = await get_skill_registry(db, tool_router)
    await skill_registry.load_installed_skills()

    if settings.signal_enabled:
        signal_service = get_signal_service()
        await signal_service.start()
        orchestrator = await get_orchestrator(db, tool_router, task_runner, coding_manager)
        await signal_service.start_polling(db=db, orchestrator=orchestrator)

    if settings.heartbeat_enabled:
        heartbeat_service = await resolve_heartbeat_service(db)
        await heartbeat_service.start()

    if settings.dream_enabled:
        dream_service = await resolve_dream_service(db)
        await dream_service.start()

    if settings.awareness_enabled:
        awareness_service = await resolve_awareness_service(db)
        await awareness_service.start()

    # Watched Shells subsystem
    shell_worker = None
    shell_notifier = None
    shell_extractor = None
    if settings.shells_enabled:
        shell_service = await resolve_shell_service(db)
        try:
            await shell_service.discover_existing()
        except Exception as exc:  # pragma: no cover
            startup_logger.debug("shells discover failed: %s", exc)
        from aria.tools.builtin import SendShellInputTool
        try:
            tool_router.register_tool(SendShellInputTool(shell_service))
        except ValueError:
            pass
        from aria.shells.snapshot import SnapshotWorker
        shell_worker = SnapshotWorker(shell_service)
        await shell_worker.start()
        app.state.shell_worker = shell_worker

        if settings.shells_idle_notifier_enabled:
            from aria.shells.notifier import IdleNotifier
            shell_notifier = IdleNotifier(shell_service, get_notification_service())
            await shell_notifier.start()
            app.state.shell_notifier = shell_notifier

        if settings.shells_extraction_enabled:
            from aria.shells.extraction import ShellExtractionWorker
            from aria.memory.extraction import MemoryExtractor
            shell_extractor = ShellExtractionWorker(
                shell_service, MemoryExtractor(db)
            )
            await shell_extractor.start()
            app.state.shell_extractor = shell_extractor

        if settings.shells_prune_enabled:
            from aria.shells.prune import ShellEventsPruneWorker
            shell_pruner = ShellEventsPruneWorker(db)
            await shell_pruner.start()
            app.state.shell_pruner = shell_pruner

        if settings.shells_reap_enabled:
            from aria.shells.reaper import ShellReaperWorker
            shell_reaper = ShellReaperWorker(shell_service, get_notification_service())
            await shell_reaper.start()
            app.state.shell_reaper = shell_reaper

        if settings.selfcheck_enabled:
            from aria.shells.selfcheck import SelfCheckWorker
            selfcheck = SelfCheckWorker(
                db,
                get_notification_service(),
                interval_minutes=settings.selfcheck_interval_minutes,
                cooldown_minutes=settings.selfcheck_alert_cooldown_minutes,
            )
            await selfcheck.start()
            app.state.selfcheck = selfcheck

        if settings.report_enabled:
            from aria.shells.report import HeartbeatReportWorker
            report_worker = HeartbeatReportWorker(
                db,
                get_notification_service(),
                weekday=settings.report_weekday,
                hour=settings.report_hour,
            )
            await report_worker.start()
            app.state.report_worker = report_worker

        if settings.projects_harvest_enabled:
            from aria.shells.harvest import ProjectHarvestWorker
            project_harvester = ProjectHarvestWorker(
                db, interval_minutes=settings.projects_harvest_interval_minutes
            )
            await project_harvester.start()
            app.state.project_harvester = project_harvester

        if settings.shared_scan_enabled:
            import socket
            from aria.shared.scan import ScanReconcileWorker, MachineScanMemoryEmitter, GitChangeEmitter
            node_id = settings.local_node_id or socket.gethostname()
            emitters = [MachineScanMemoryEmitter(node_id)]
            if settings.git_scan_enabled:
                from aria.shells.harvest import DEFAULT_ROOTS, EXTRA_REPO_ROOTS
                git_roots = settings.git_scan_roots or (DEFAULT_ROOTS + EXTRA_REPO_ROOTS)
                emitters.append(GitChangeEmitter(git_roots, settings.git_scan_min_change_lines))
            # Ontology projection (Phase 5a). Rides this worker rather than
            # adding a second scanner; opts into always_run because its inputs
            # (registries, db.projects) change without the machine snapshot
            # changing. LLM-free, and embedding is off by default.
            if settings.ontology_enabled:
                from aria.ontology.emitter import OntologyProjectionEmitter
                emitters.append(
                    OntologyProjectionEmitter(embed=settings.ontology_projection_embed)
                )
            scan_worker = ScanReconcileWorker(
                db,
                emitters=emitters,
                interval_seconds=settings.shared_scan_interval_seconds,
                node_id=node_id,
            )
            await scan_worker.start()
            app.state.scan_worker = scan_worker

        if settings.shells_adopt_enabled:
            from aria.shells.adopt import ShellAdoptWorker
            shell_adopter = ShellAdoptWorker(shell_service)
            await shell_adopter.start()
            app.state.shell_adopter = shell_adopter

    # Coherence C3: Linear backlog sync + reconciliation (per-project opt-in
    # via linear_project_map; auto-resolve threshold-gated, logged, reversible).
    if settings.linear_enabled and settings.linear_api_key:
        from aria.planning.linear_sync import LinearSyncWorker
        linear_sync = LinearSyncWorker(db, get_notification_service())
        await linear_sync.start()
        app.state.linear_sync = linear_sync

    # Relay watchdog (steward plan §6.4). ARIA enqueues alerts and Hermes relays
    # them; that relay has died silently three times (2026-06-29, 07-28, 08-10 —
    # the last left 31 alerts undelivered for five days) and nothing noticed.
    # Deliberately NOT inside the `shells_enabled` block: it watches delivery,
    # not the fleet, and must keep running when the fleet is off.
    if settings.alert_relay_watchdog_enabled:
        from aria.notifications.relay import RelayWatchdog
        relay_watchdog = RelayWatchdog(db, get_notification_service())
        await relay_watchdog.start()
        app.state.relay_watchdog = relay_watchdog

    # Vault reader (steward plan §3.2). The vault stops being write-only here:
    # Ben edits `approval:` / `autonomy:` / `accepted:` on his phone, LiveSync
    # lands it on this disk within seconds, and this is the thing that reads it
    # back. Without it the shared notepad is a broadcast, not a control surface.
    # ---- Steward layer -----------------------------------------------------
    # Order matters here: the StewardWorker is constructed BEFORE the VaultReader
    # because the reader hands its events to `steward.handle_vault_events`. Start
    # them the other way round and Ben's approval flips are computed, delivered to
    # nobody, and lost — the file state has already advanced by then.
    if settings.steward_enabled:
        from aria.steward.service import StewardWorker
        steward_worker = StewardWorker(
            db,
            notifier=get_notification_service(),
            coding_manager=coding_manager,
            shell_service=await resolve_shell_service(db),
        )
        await steward_worker.start()
        app.state.steward = steward_worker

    if settings.vault_reader_enabled:
        from aria.integrations.vault_reader import VaultReader
        vault_reader = VaultReader(
            db=db, interval_seconds=settings.vault_reader_interval_seconds
        )
        steward_worker = getattr(app.state, "steward", None)
        if steward_worker is not None:
            vault_reader.on_events = steward_worker.handle_vault_events
        else:
            # Reading the vault while nothing consumes the events would burn
            # Ben's edits: poll_once() advances each file's stored hash, so the
            # NEXT poll sees no change and the approval is gone. Refuse instead.
            startup_logger.warning(
                "vault_reader_enabled but steward_enabled is false — not starting the "
                "reader, because polling with no consumer silently consumes Ben's edits"
            )
            vault_reader = None
        if vault_reader is not None:
            await vault_reader.start()
            app.state.vault_reader = vault_reader

    # The supervisor watches every agent kind and owns the escalation ladder. It
    # is what turns "a session stalled" from a log line into an action.
    if settings.meta_supervisor_enabled:
        from aria.steward.supervisor import MetaSupervisor
        meta_supervisor = MetaSupervisor(
            db,
            session_manager=coding_manager,
            notification_service=get_notification_service(),
            watchdog=watchdog,
        )
        await meta_supervisor.start()
        app.state.meta_supervisor = meta_supervisor

    # Triage: classify, diagnose, propose — the loop that used to be a Hermes
    # cron prompt run by a 4B model, and died with it on 2026-08-10.
    if getattr(settings, "triage_enabled", False):
        from aria.notifications.triage import TriageWorker
        triage_worker = TriageWorker(
            db, get_notification_service(), manager=coding_manager
        )
        await triage_worker.start()
        app.state.triage_worker = triage_worker

    # The paused-shell nudger's TIMER (its state and three-strikes bookkeeping
    # were always ARIA-side; only the clock lived in Hermes).
    if getattr(settings, "shells_nudge_worker_enabled", False):
        from aria.shells.nudge_worker import NudgeWorker
        nudge_worker = NudgeWorker(
            db,
            shell_service=await resolve_shell_service(db),
            notifier=get_notification_service(),
        )
        await nudge_worker.start()
        app.state.nudge_worker = nudge_worker

    # Outcomes: without a label on each finished session there is no metric, and
    # without a metric the improvement loop below has nothing to gate on.
    if settings.outcome_scoring_enabled:
        from aria.steward.outcomes import OutcomeWorker
        outcome_worker = OutcomeWorker(db, notifier=get_notification_service())
        await outcome_worker.start()
        app.state.outcome_worker = outcome_worker

    if getattr(settings, "research_planner_enabled", False):
        from aria.steward.research import ResearchPlanner
        research_planner = ResearchPlanner(db, notifier=get_notification_service())
        await research_planner.start()
        app.state.research_planner = research_planner

    if settings.improver_enabled:
        from aria.steward.improve import Improver
        improver = Improver(db, notifier=get_notification_service())
        await improver.start()
        app.state.improver = improver

    # Planning subsystem (tasks + projects) — index bootstrap. Cheap, idempotent.
    try:
        await db.tasks.create_index([("status", 1), ("updated_at", -1)])
        await db.tasks.create_index([("project_id", 1), ("status", 1)])
        await db.tasks.create_index([("due_at", 1)], sparse=True)
        await db.tasks.create_index(
            [("content_hash", 1)],
            partialFilterExpression={"status": {"$in": ["proposed", "active"]}},
        )
        await db.projects.create_index([("slug", 1)], unique=True)
        await db.projects.create_index([("status", 1), ("last_signal_at", -1)])
        await db.alerts.create_index([("acked", 1), ("created_at", -1)])
        # Alerts v2 read paths: the relay selects needs_human+undelivered every
        # 5 minutes, and dedup does a keyed lookup on every single notify().
        await db.alerts.create_index([("dedup_key", 1), ("acked", 1)])
        await db.alerts.create_index([("needs_human", 1), ("acked", 1), ("created_at", -1)])
        await db.alerts.create_index([("delivered_at", 1)], sparse=True)
        # Bounded life for the info lane. Coding-session lifecycle rows are
        # never acked, never delivered and never read by anything that closes
        # them, so before this they accumulated forever — inflating every
        # project's cockpit attention score and eventually crowding the real
        # alerts out of the cockpit's 300-row read. notifications/service.py
        # sets `expires_at` on info rows only and clears it the moment one
        # escalates; a TTL index ignores documents whose field is null or
        # missing, so nothing that needs a human is reachable by this sweep.
        await db.alerts.create_index([("expires_at", 1)], expireAfterSeconds=0, sparse=True)
        startup_logger.info("Planning indexes ready")
    except Exception as exc:  # pragma: no cover - non-fatal
        startup_logger.warning("Planning index creation failed: %s", exc)

    yield

    # Shutdown — graceful drain of in-flight work
    import asyncio
    import logging

    shutdown_logger = logging.getLogger("aria.shutdown")
    shutdown_logger.info("Initiating graceful shutdown...")

    # 1. Stop accepting new scheduled work
    from aria.api.deps import (
        _scheduler, _task_runner, _rate_limit_watchdog, _escalation_manager,
    )
    if _scheduler is not None:
        await _scheduler.stop()
    if _rate_limit_watchdog is not None:
        await _rate_limit_watchdog.stop()
    if _escalation_manager is not None:
        await _escalation_manager.stop()

    # 2. Drain in-flight background tasks (up to 10s)
    if _task_runner is not None:
        pending = _task_runner.get_running_tasks() if hasattr(_task_runner, "get_running_tasks") else []
        if pending:
            shutdown_logger.info("Waiting for %d in-flight task(s) to complete...", len(pending))
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[t for t in pending if not t.done()], return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                shutdown_logger.warning("Timed out waiting for tasks; cancelling remaining")
                for t in pending:
                    if not t.done():
                        t.cancel()

    # 3. Stop dream cycle and awareness
    from aria.api.deps import _dream_service, _awareness_service
    if _dream_service is not None:
        await _dream_service.stop()
    if _awareness_service is not None:
        await _awareness_service.stop()

    # 3a. Stop watched shells workers
    for attr in (
        "shell_notifier", "shell_extractor", "shell_pruner", "shell_reaper",
        "project_harvester", "scan_worker", "selfcheck", "report_worker",
        "shell_adopter", "shell_worker", "linear_sync", "embedding_backfill",
        "relay_watchdog", "vault_reader", "steward", "meta_supervisor",
        "triage_worker", "nudge_worker", "outcome_worker", "research_planner",
        "improver",
    ):
        worker = getattr(app.state, attr, None)
        if worker is not None:
            try:
                await worker.stop()
            except Exception as exc:  # pragma: no cover
                shutdown_logger.debug("shells %s stop failed: %s", attr, exc)

    # 4. Stop heartbeat
    from aria.api.deps import _heartbeat_service
    if _heartbeat_service is not None:
        await _heartbeat_service.stop()

    # 4. Stop Signal polling
    signal_service = get_signal_service()
    await signal_service.shutdown()

    # 5. Shut down MCP servers
    mcp_manager = get_mcp_manager()
    await mcp_manager.shutdown_all()

    # 6. Close LLM adapter HTTP clients
    from aria.llm.manager import llm_manager as _llm_mgr
    await _llm_mgr.close_all()

    # 7. Close embedding service HTTP clients
    from aria.memory.embeddings import embedding_service
    await embedding_service.close()

    # 8. Close database connection last
    await close_db()
    shutdown_logger.info("Shutdown complete")


app = FastAPI(
    title="ARIA",
    description="Autonomous Reasoning & Intelligence Architecture",
    version="0.2.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Assign a correlation ID to each request for end-to-end tracing."""
    from aria.core.logging import set_correlation_id
    cid = request.headers.get("X-Correlation-ID") or None
    cid = set_correlation_id(cid)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = cid
    return response


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Apply request-level rate limiting."""
    if request.method == "OPTIONS":
        return await call_next(request)

    public_prefixes = ("/docs", "/openapi.json", "/redoc", "/api/v1/health")
    if request.url.path == "/" or request.url.path.startswith(public_prefixes):
        return await call_next(request)

    rate_limiter = get_rate_limiter()
    client_key = request.headers.get("X-API-Key") or (request.client.host if request.client else "unknown")
    allowed, remaining = rate_limiter.check(f"{client_key}:{request.url.path}")
    if not allowed:
        # Best-effort audit — a DB failure here must NOT turn the intended 429
        # into a 500.
        try:
            db = await get_database()
            audit = await get_audit_service(db)
            await audit.log_event(
                category="security",
                action="rate_limit",
                status="blocked",
                actor=client_key,
                target=request.url.path,
                metadata={"method": request.method},
            )
        except Exception:
            import logging as _logging
            _logging.getLogger("aria.middleware").warning(
                "Failed to write rate-limit audit event", exc_info=True
            )
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

    response = await call_next(request)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return response


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """Optional API key auth for remote access."""
    if request.method == "OPTIONS":
        return await call_next(request)

    if not settings.api_auth_enabled:
        return await call_next(request)

    public_prefixes = ("/docs", "/openapi.json", "/redoc", "/api/v1/health")
    if request.url.path == "/" or request.url.path.startswith(public_prefixes):
        return await call_next(request)

    # Query-param fallback: browser EventSource cannot set custom headers, so
    # SSE endpoints (e.g. /shells/{name}/stream) pass the key this way instead.
    #
    # Bearer fallback (2026-08-05): the /llm/v1 OpenAI-compatible proxy is meant
    # to be usable by stock OpenAI clients, which only know how to send
    # `Authorization: Bearer <key>` — they cannot set X-API-Key. Accepting the
    # same key either way keeps auth on (this app binds 0.0.0.0:8200) while
    # letting a caller just set its api_key to the ARIA key.
    provided = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if not provided:
        auth_header = request.headers.get("Authorization") or ""
        if auth_header.lower().startswith("bearer "):
            provided = auth_header[7:].strip()
    if not settings.api_key or not hmac.compare_digest(provided or "", settings.api_key):
        # Best-effort audit — a DB failure must NOT turn the intended 401 into a 500.
        try:
            db = await get_database()
            audit = await get_audit_service(db)
            await audit.log_event(
                category="security",
                action="api_auth",
                status="denied",
                actor=request.client.host if request.client else "unknown",
                target=request.url.path,
                metadata={"method": request.method},
            )
        except Exception:
            import logging as _logging
            _logging.getLogger("aria.middleware").warning(
                "Failed to write api-auth audit event", exc_info=True
            )
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


# CORS middleware for web UI — added LAST so it is OUTERMOST in the stack
# (Starlette's add_middleware() makes the most-recently-added middleware wrap
# everything registered before it). It must wrap correlation_id/rate_limit/
# api_key_middleware, not sit inside them: those three can short-circuit with
# a direct JSONResponse (401/429) that skips call_next entirely, and a
# response built inside CORSMiddleware never passes back out through it — the
# browser then sees a headerless response and reports a CORS failure instead
# of the real 401/429. This bit a real feature: the shells live-stream page
# uses a browser EventSource, which cannot set the X-API-Key header, so every
# such connection hit exactly this — a 401 with no CORS headers, surfaced to
# users as an opaque "blocked by CORS policy" error.
# Allow common development and deployment origins
# For Docker: The UI service can access API via internal Docker network
# For external access: Adjust these origins based on your deployment
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        *settings.cors_origins,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_origin_regex=r"http://.*:(3000|1420)",  # Allow any host on port 3000 or 1420
)

# Include routers
app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(conversations.router, prefix="/api/v1", tags=["conversations"])
app.include_router(agents.router, prefix="/api/v1", tags=["agents"])
app.include_router(memories.router, prefix="/api/v1", tags=["memories"])
app.include_router(memory_api.router, prefix="/api/v1", tags=["memory"])
app.include_router(capabilities.router, prefix="/api/v1", tags=["capabilities"])
app.include_router(shared.router, prefix="/api/v1", tags=["shared"])
app.include_router(usage.router, prefix="/api/v1", tags=["usage"])
app.include_router(signal.router, prefix="/api/v1", tags=["signal"])
app.include_router(notifications.router, prefix="/api/v1", tags=["notifications"])
app.include_router(tasks.router, prefix="/api/v1", tags=["tasks"])
app.include_router(research.router, prefix="/api/v1", tags=["research"])
app.include_router(coding_sessions.router, prefix="/api/v1", tags=["coding"])
app.include_router(routing.router, prefix="/api/v1", tags=["routing"])
app.include_router(infrastructure.router, prefix="/api/v1", tags=["infrastructure"])
app.include_router(ontology.router, prefix="/api/v1", tags=["ontology"])
app.include_router(benchmarks.router, prefix="/api/v1", tags=["benchmarks"])
app.include_router(workflows.router, prefix="/api/v1", tags=["workflows"])
app.include_router(schedules.router, prefix="/api/v1", tags=["schedules"])
app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
app.include_router(tools.router, prefix="/api/v1", tags=["tools"])
# Mounted at the ROOT, not /api/v1, so the path ends in a bare `/v1` and any
# stock OpenAI client works against http://<host>:8200/llm/v1 unmodified.
# This is what LLAMACPP_URL points at, so "the local model" resolves through
# ARIA's registry instead of a hardcoded port that goes stale.
app.include_router(llm_proxy.router, tags=["llm-proxy"])
# Same proxy plus a system line naming the resident model. Deliberately a
# separate base_url so /llm/v1 (= LLAMACPP_URL, which evalstack and the
# benchmark routes use) keeps forwarding request bodies verbatim.
app.include_router(llm_proxy.identified_router, tags=["llm-proxy"])
app.include_router(tts.router, prefix="/api/v1", tags=["tts"])
app.include_router(stt.router, prefix="/api/v1", tags=["stt"])
app.include_router(killswitch.router, prefix="/api/v1", tags=["killswitch"])
app.include_router(skills.router, prefix="/api/v1", tags=["skills"])
app.include_router(groupchat.router, prefix="/api/v1", tags=["groupchat"])
app.include_router(autopilot.router, prefix="/api/v1", tags=["autopilot"])
app.include_router(heartbeat.router, prefix="/api/v1", tags=["heartbeat"])
app.include_router(dreams.router, prefix="/api/v1", tags=["dreams"])
app.include_router(awareness.router, prefix="/api/v1", tags=["awareness"])
app.include_router(shells.router, prefix="/api/v1", tags=["shells"])
app.include_router(shell_nudge.router, prefix="/api/v1", tags=["shells"])
app.include_router(obsidian.router, prefix="/api/v1", tags=["obsidian"])
app.include_router(linear.router, prefix="/api/v1", tags=["linear"])
app.include_router(nodes.router, prefix="/api/v1", tags=["nodes"])
# digest MUST register before planning: its literal /projects/{overview,active}
# paths would otherwise be captured by planning's /projects/{project_id}.
app.include_router(digest.router, prefix="/api/v1", tags=["cockpit"])
app.include_router(planning.router, prefix="/api/v1", tags=["planning"])
app.include_router(alerts.router, prefix="/api/v1", tags=["alerts"])
app.include_router(guard.router, prefix="/api/v1", tags=["guard"])
app.include_router(steward.router, prefix="/api/v1", tags=["steward"])
app.include_router(improve.router, prefix="/api/v1", tags=["improve"])


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "ARIA",
        "version": "0.2.0",
        "docs": "/docs",
    }
