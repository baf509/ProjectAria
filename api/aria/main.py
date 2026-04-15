"""
ARIA - Main FastAPI Application

Phase: 1, 3
Purpose: FastAPI application entry point

Related Spec Sections:
- Section 5: API Specification
- Section 7: Project Structure
"""

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
from aria.api.routes import admin, health, conversations, agents, memories, tools, tts, stt, usage, signal, notifications, tasks, research, coding_sessions, infrastructure, workflows, schedules, killswitch, skills, groupchat, autopilot, telegram, heartbeat, dreams, awareness, shells
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
    get_telegram_handler,
    get_tool_router,
    resolve_coding_watchdog,
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
    ListLlamaCppModelsTool,
    PiCodingAgentTool,
    ScreenshotTool,
    SendToCodingSessionTool,
    ShellTool,
    SoulTool,
    StartCodingSessionTool,
    StopCodingSessionTool,
    SwitchLlamaCppModelTool,
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

    # Check embedding service
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
    for backend_name in ("llamacpp", "anthropic", "openai", "openrouter"):
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
    tool_router.register_tool(ListLlamaCppModelsTool())
    tool_router.register_tool(SendToCodingSessionTool(coding_manager))
    tool_router.register_tool(
        ShellTool(
            allowed_commands=settings.shell_allowed_commands,
            denied_commands=settings.shell_denied_commands,
            working_directory=settings.coding_default_workspace,
        )
    )
    tool_router.register_tool(StartCodingSessionTool(coding_manager))
    tool_router.register_tool(StopCodingSessionTool(coding_manager))
    tool_router.register_tool(SwitchLlamaCppModelTool())
    tool_router.register_tool(WebTool())
    tool_router.register_tool(ScreenshotTool())
    tool_router.register_tool(DocumentGenerationTool())
    tool_router.register_tool(SoulTool())
    if ClaudeRunner.is_available():
        tool_router.register_tool(ClaudeAgentTool())
        tool_router.register_tool(DeepThinkTool())
        startup_logger.info("Claude Agent + Deep Think tools registered (CLI available)")

    # Register Pi Coding Agent tool (uses local LLM via orchestrator)
    tool_router.register_tool(PiCodingAgentTool(db))
    startup_logger.info("Pi Coding Agent tool registered")

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
    await task_runner.recover_stale_tasks()

    scheduler = await resolve_scheduler(db, task_runner)
    await scheduler.start()

    # Load killswitch state
    ks = get_killswitch()
    await ks.load_state(db)

    # Load installed skills
    skill_registry = await get_skill_registry(db, tool_router)
    await skill_registry.load_installed_skills()

    if settings.signal_enabled:
        signal_service = get_signal_service()
        await signal_service.start()
        orchestrator = await get_orchestrator(db, tool_router, task_runner, coding_manager)
        await signal_service.start_polling(db=db, orchestrator=orchestrator)

    if settings.telegram_enabled and settings.telegram_bot_token:
        telegram_handler = get_telegram_handler()
        orchestrator = await get_orchestrator(db, tool_router, task_runner, coding_manager)
        await telegram_handler.start_polling(db=db, orchestrator=orchestrator)

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

    yield

    # Shutdown — graceful drain of in-flight work
    import asyncio
    import logging

    shutdown_logger = logging.getLogger("aria.shutdown")
    shutdown_logger.info("Initiating graceful shutdown...")

    # 1. Stop accepting new scheduled work
    from aria.api.deps import _scheduler, _task_runner
    if _scheduler is not None:
        await _scheduler.stop()

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
    for attr in ("shell_notifier", "shell_extractor", "shell_worker"):
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

    # 4. Stop Telegram polling
    if settings.telegram_enabled:
        telegram_handler = get_telegram_handler()
        await telegram_handler.stop_polling()

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

# CORS middleware for web UI
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

    provided = request.headers.get("X-API-Key")
    if not settings.api_key or provided != settings.api_key:
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
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)

# Include routers
app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(conversations.router, prefix="/api/v1", tags=["conversations"])
app.include_router(agents.router, prefix="/api/v1", tags=["agents"])
app.include_router(memories.router, prefix="/api/v1", tags=["memories"])
app.include_router(usage.router, prefix="/api/v1", tags=["usage"])
app.include_router(signal.router, prefix="/api/v1", tags=["signal"])
app.include_router(notifications.router, prefix="/api/v1", tags=["notifications"])
app.include_router(tasks.router, prefix="/api/v1", tags=["tasks"])
app.include_router(research.router, prefix="/api/v1", tags=["research"])
app.include_router(coding_sessions.router, prefix="/api/v1", tags=["coding"])
app.include_router(infrastructure.router, prefix="/api/v1", tags=["infrastructure"])
app.include_router(workflows.router, prefix="/api/v1", tags=["workflows"])
app.include_router(schedules.router, prefix="/api/v1", tags=["schedules"])
app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
app.include_router(tools.router, prefix="/api/v1", tags=["tools"])
app.include_router(tts.router, prefix="/api/v1", tags=["tts"])
app.include_router(stt.router, prefix="/api/v1", tags=["stt"])
app.include_router(killswitch.router, prefix="/api/v1", tags=["killswitch"])
app.include_router(skills.router, prefix="/api/v1", tags=["skills"])
app.include_router(groupchat.router, prefix="/api/v1", tags=["groupchat"])
app.include_router(autopilot.router, prefix="/api/v1", tags=["autopilot"])
app.include_router(telegram.router, prefix="/api/v1", tags=["telegram"])
app.include_router(heartbeat.router, prefix="/api/v1", tags=["heartbeat"])
app.include_router(dreams.router, prefix="/api/v1", tags=["dreams"])
app.include_router(awareness.router, prefix="/api/v1", tags=["awareness"])
app.include_router(shells.router, prefix="/api/v1", tags=["shells"])


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "ARIA",
        "version": "0.2.0",
        "docs": "/docs",
    }
