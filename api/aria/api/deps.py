"""
ARIA - API Dependencies

Phase: 1, 3
Purpose: Dependency injection for FastAPI routes

Related Spec Sections:
- Section 9.4: Dependency Injection
"""

from typing import Annotated, Optional
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Depends, Header, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from aria.db.mongodb import get_database
from aria.core.orchestrator import Orchestrator
from aria.infrastructure.model_pull import ModelPullService
from aria.infrastructure.model_servers import ModelServerManager
from aria.infrastructure.services import ServiceManager
from aria.research.service import ResearchService
from aria.agents.session import CodingSessionManager
from aria.agents.review import CodingReviewService
from aria.agents.watchdog import CodingWatchdog
from aria.tasks.runner import TaskRunner
from aria.tools.router import ToolRouter
from aria.tools.mcp.manager import MCPManager
from aria.notifications.service import NotificationService
from aria.security.audit import AuditService
from aria.security.rate_limit import RateLimiter
from aria.signal.service import SignalService
from aria.workflows.engine import WorkflowEngine
from aria.scheduler.service import SchedulerService
from aria.core.killswitch import Killswitch
from aria.skills.registry import SkillRegistry
from aria.groupchat.service import GroupChatService
from aria.autopilot.service import AutopilotService
from aria.heartbeat.service import HeartbeatService
from aria.awareness.service import AwarenessService
from aria.dreams.service import DreamService
from aria.agents.estop import EstopManager, RateLimitWatchdog
from aria.agents.mail import AgentMailbox
from aria.notifications.escalation import EscalationManager
from aria.shells.service import ShellService
from aria.nodes.service import NodeService
from aria.planning.service import PlanningService

def valid_object_id(value: str) -> ObjectId:
    """Validate and convert a string to a BSON ObjectId, raising 400 on invalid input."""
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid ID format: {value}")


# Global instances
_tool_router: ToolRouter = None
_mcp_manager: MCPManager = None
_signal_service: SignalService = None
_notification_service: NotificationService = None
_task_runner: TaskRunner = None
_research_service: ResearchService = None
_model_server_manager: ModelServerManager = None
_service_manager: ServiceManager = None
_model_pull_service: ModelPullService = None
_coding_session_manager: CodingSessionManager = None
_coding_review_service: CodingReviewService = None
_coding_watchdog: CodingWatchdog = None
_workflow_engine: WorkflowEngine = None
_scheduler: SchedulerService = None
_audit_service: AuditService = None
_rate_limiter: RateLimiter = None
_killswitch: Killswitch = None
_skill_registry: SkillRegistry = None
_groupchat_service: GroupChatService = None
_autopilot_service: AutopilotService = None
_heartbeat_service: HeartbeatService = None
_vault_reader = None  # aria.integrations.vault_reader.VaultReader
_dream_service: DreamService = None
_awareness_service: AwarenessService = None
_estop_manager: EstopManager = None
_rate_limit_watchdog: RateLimitWatchdog = None
_agent_mailbox: AgentMailbox = None
_escalation_manager: EscalationManager = None
_shell_service: ShellService = None
_node_service: NodeService = None
_planning_service: PlanningService = None


def get_tool_router() -> ToolRouter:
    """Get tool router instance."""
    global _tool_router
    if _tool_router is None:
        _tool_router = ToolRouter()
    return _tool_router


def get_mcp_manager() -> MCPManager:
    """Get MCP manager instance."""
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPManager()
    return _mcp_manager


def get_signal_service() -> SignalService:
    """Get Signal service instance."""
    global _signal_service
    if _signal_service is None:
        _signal_service = SignalService()
    return _signal_service


def get_rate_limiter() -> RateLimiter:
    """Get shared rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


async def get_db() -> AsyncIOMotorDatabase:
    """Get database instance."""
    return await get_database()


def get_notification_service() -> NotificationService:
    """Get notification service instance."""
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService(get_signal_service())
    return _notification_service


async def get_audit_service(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> AuditService:
    """Get audit service instance."""
    global _audit_service
    if _audit_service is None:
        _audit_service = AuditService(db)
    else:
        _audit_service.db = db
    return _audit_service


def get_model_server_manager() -> ModelServerManager:
    """Get shared model-server registry/control-plane manager."""
    global _model_server_manager
    if _model_server_manager is None:
        _model_server_manager = ModelServerManager()
    return _model_server_manager


def get_service_manager() -> ServiceManager:
    """Get shared non-LLM service registry manager.

    Deliberately separate from get_model_server_manager — see the module
    docstring in aria/infrastructure/services.py for why the two registries
    must not merge.
    """
    global _service_manager
    if _service_manager is None:
        _service_manager = ServiceManager()
    return _service_manager


def get_model_pull_service() -> ModelPullService:
    """Get shared Hugging Face model pull/provisioning service."""
    global _model_pull_service
    if _model_pull_service is None:
        _model_pull_service = ModelPullService()
    return _model_pull_service


async def get_task_runner(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> TaskRunner:
    """Get background task runner instance."""
    global _task_runner
    if _task_runner is None:
        _task_runner = TaskRunner(db, get_notification_service())
    else:
        _task_runner.db = db
    return _task_runner


async def get_research_service(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
) -> ResearchService:
    """Get research service instance."""
    global _research_service
    if _research_service is None:
        _research_service = ResearchService(db, task_runner)
    else:
        _research_service.db = db
        _research_service.task_runner = task_runner
    return _research_service


async def get_coding_session_manager(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> CodingSessionManager:
    """Get coding session manager instance."""
    global _coding_session_manager
    if _coding_session_manager is None:
        _coding_session_manager = CodingSessionManager(db, get_notification_service())
    else:
        _coding_session_manager.db = db
    return _coding_session_manager


async def get_coding_review_service(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    manager: Annotated[CodingSessionManager, Depends(get_coding_session_manager)],
) -> CodingReviewService:
    """Get coding review service instance."""
    global _coding_review_service
    if _coding_review_service is None:
        _coding_review_service = CodingReviewService(db, manager)
    else:
        _coding_review_service.db = db
        _coding_review_service.session_manager = manager
    return _coding_review_service


async def get_coding_watchdog(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    manager: Annotated[CodingSessionManager, Depends(get_coding_session_manager)],
    review_service: Annotated[CodingReviewService, Depends(get_coding_review_service)],
) -> CodingWatchdog:
    """Get coding session watchdog instance."""
    global _coding_watchdog
    if _coding_watchdog is None:
        _coding_watchdog = CodingWatchdog(db, manager, get_notification_service(), review_service)
    else:
        _coding_watchdog.db = db
        _coding_watchdog.session_manager = manager
        _coding_watchdog.review_service = review_service
    return _coding_watchdog


async def resolve_coding_watchdog(
    db: AsyncIOMotorDatabase,
    manager: CodingSessionManager,
) -> CodingWatchdog:
    """Resolve watchdog outside FastAPI dependency injection."""
    global _coding_watchdog, _coding_review_service
    if _coding_review_service is None:
        _coding_review_service = CodingReviewService(db, manager)
    else:
        _coding_review_service.db = db
        _coding_review_service.session_manager = manager

    if _coding_watchdog is None:
        _coding_watchdog = CodingWatchdog(db, manager, get_notification_service(), _coding_review_service)
    else:
        _coding_watchdog.db = db
        _coding_watchdog.session_manager = manager
        _coding_watchdog.review_service = _coding_review_service
    return _coding_watchdog


async def get_orchestrator(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    tool_router: Annotated[ToolRouter, Depends(get_tool_router)],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
    coding_manager: Annotated[CodingSessionManager, Depends(get_coding_session_manager)],
) -> Orchestrator:
    """Get orchestrator instance."""
    return Orchestrator(db, tool_router, task_runner=task_runner, coding_manager=coding_manager)


async def get_scheduler(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
) -> SchedulerService:
    """Get scheduler service instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerService(
            db=db,
            task_runner=task_runner,
            notification_service=get_notification_service(),
        )
    else:
        _scheduler.db = db
        _scheduler.task_runner = task_runner
    return _scheduler


async def resolve_scheduler(
    db: AsyncIOMotorDatabase,
    task_runner: TaskRunner,
) -> SchedulerService:
    """Resolve scheduler outside FastAPI dependency injection."""
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerService(
            db=db,
            task_runner=task_runner,
            notification_service=get_notification_service(),
        )
    else:
        _scheduler.db = db
        _scheduler.task_runner = task_runner
    return _scheduler


def get_killswitch() -> Killswitch:
    """Get killswitch singleton."""
    global _killswitch
    if _killswitch is None:
        _killswitch = Killswitch()
    return _killswitch


async def get_skill_registry(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    tool_router: Annotated[ToolRouter, Depends(get_tool_router)],
) -> SkillRegistry:
    """Get skill registry instance."""
    global _skill_registry
    if _skill_registry is None:
        _skill_registry = SkillRegistry(db, tool_router)
    else:
        _skill_registry.db = db
    return _skill_registry


async def get_groupchat_service(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> GroupChatService:
    """Get group chat service instance."""
    global _groupchat_service
    if _groupchat_service is None:
        _groupchat_service = GroupChatService(db)
    else:
        _groupchat_service.db = db
    return _groupchat_service


async def get_autopilot_service(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
    tool_router: Annotated[ToolRouter, Depends(get_tool_router)],
) -> AutopilotService:
    """Get autopilot service instance."""
    global _autopilot_service
    killswitch = get_killswitch()
    if _autopilot_service is None:
        _autopilot_service = AutopilotService(db, killswitch, task_runner, tool_router)
    else:
        _autopilot_service.db = db
        _autopilot_service.task_runner = task_runner
    return _autopilot_service


async def get_heartbeat_service(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> HeartbeatService:
    """Get heartbeat service instance."""
    global _heartbeat_service
    if _heartbeat_service is None:
        _heartbeat_service = HeartbeatService(db, get_notification_service())
    else:
        _heartbeat_service.db = db
    return _heartbeat_service


async def resolve_heartbeat_service(
    db: AsyncIOMotorDatabase,
) -> HeartbeatService:
    """Resolve heartbeat service outside FastAPI dependency injection."""
    global _heartbeat_service
    if _heartbeat_service is None:
        _heartbeat_service = HeartbeatService(db, get_notification_service())
    else:
        _heartbeat_service.db = db
    return _heartbeat_service


async def get_workflow_engine(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
    task_runner: Annotated[TaskRunner, Depends(get_task_runner)],
    tool_router: Annotated[ToolRouter, Depends(get_tool_router)],
    research_service: Annotated[ResearchService, Depends(get_research_service)],
    coding_manager: Annotated[CodingSessionManager, Depends(get_coding_session_manager)],
) -> WorkflowEngine:
    """Get workflow engine instance."""
    global _workflow_engine
    if _workflow_engine is None:
        _workflow_engine = WorkflowEngine(
            db=db,
            task_runner=task_runner,
            tool_router=tool_router,
            notification_service=get_notification_service(),
            research_service=research_service,
            coding_manager=coding_manager,
        )
    else:
        _workflow_engine.db = db
        _workflow_engine.task_runner = task_runner
        _workflow_engine.tool_router = tool_router
        _workflow_engine.research_service = research_service
        _workflow_engine.coding_manager = coding_manager
    return _workflow_engine


async def resolve_dream_service(
    db: AsyncIOMotorDatabase,
) -> DreamService:
    """Resolve dream service outside FastAPI dependency injection."""
    global _dream_service
    if _dream_service is None:
        _dream_service = DreamService(db)
    else:
        _dream_service.db = db
    return _dream_service


async def get_awareness_service(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> AwarenessService:
    """Get awareness service instance."""
    global _awareness_service
    if _awareness_service is None:
        _awareness_service = AwarenessService(db)
    else:
        _awareness_service.db = db
    return _awareness_service


async def resolve_awareness_service(
    db: AsyncIOMotorDatabase,
) -> AwarenessService:
    """Resolve awareness service outside FastAPI dependency injection."""
    global _awareness_service
    if _awareness_service is None:
        _awareness_service = AwarenessService(db)
    else:
        _awareness_service.db = db
    return _awareness_service


async def get_estop_manager(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> EstopManager:
    """Get emergency stop manager instance."""
    global _estop_manager
    if _estop_manager is None:
        _estop_manager = EstopManager(db)
    else:
        _estop_manager.db = db
    return _estop_manager


async def resolve_estop_manager(
    db: AsyncIOMotorDatabase,
) -> EstopManager:
    """Resolve estop manager outside FastAPI dependency injection."""
    global _estop_manager
    if _estop_manager is None:
        _estop_manager = EstopManager(db)
    else:
        _estop_manager.db = db
    return _estop_manager


async def resolve_rate_limit_watchdog(
    db: AsyncIOMotorDatabase,
) -> RateLimitWatchdog:
    """Resolve rate limit watchdog outside FastAPI dependency injection."""
    global _rate_limit_watchdog, _estop_manager, _escalation_manager
    if _estop_manager is None:
        _estop_manager = EstopManager(db)
    if _escalation_manager is None:
        _escalation_manager = EscalationManager(db, get_notification_service())
    if _rate_limit_watchdog is None:
        _rate_limit_watchdog = RateLimitWatchdog(
            db, _estop_manager, get_notification_service(), _escalation_manager
        )
    return _rate_limit_watchdog


async def get_agent_mailbox(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> AgentMailbox:
    """Get agent mailbox instance."""
    global _agent_mailbox
    if _agent_mailbox is None:
        _agent_mailbox = AgentMailbox(db)
    else:
        _agent_mailbox.db = db
    return _agent_mailbox


async def get_escalation_manager(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> EscalationManager:
    """Get escalation manager instance."""
    global _escalation_manager
    if _escalation_manager is None:
        _escalation_manager = EscalationManager(db, get_notification_service())
    else:
        _escalation_manager.db = db
    return _escalation_manager


async def get_shell_service(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> ShellService:
    """Get the watched-shells service instance."""
    global _shell_service
    if _shell_service is None:
        _shell_service = ShellService(db)
    else:
        _shell_service.db = db
        _shell_service.shells = db.shells
        _shell_service.events = db.shell_events
        _shell_service.snapshots = db.shell_snapshots
    return _shell_service


async def get_node_service(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> NodeService:
    """Get the multi-machine node service instance."""
    global _node_service
    if _node_service is None:
        _node_service = NodeService(db)
    else:
        _node_service.db = db
        _node_service.nodes = db.nodes
        _node_service.shell_service.db = db
    return _node_service


async def resolve_shell_service(
    db: AsyncIOMotorDatabase,
) -> ShellService:
    """Resolve shell service outside FastAPI dependency injection."""
    global _shell_service
    if _shell_service is None:
        _shell_service = ShellService(db)
    else:
        _shell_service.db = db
        _shell_service.shells = db.shells
        _shell_service.events = db.shell_events
        _shell_service.snapshots = db.shell_snapshots
    return _shell_service


async def resolve_escalation_manager(
    db: AsyncIOMotorDatabase,
) -> EscalationManager:
    """Resolve escalation manager outside FastAPI dependency injection."""
    global _escalation_manager
    if _escalation_manager is None:
        _escalation_manager = EscalationManager(db, get_notification_service())
    else:
        _escalation_manager.db = db
    return _escalation_manager


async def get_planning_service(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
) -> PlanningService:
    """Get the planning (tasks + projects) service instance."""
    global _planning_service
    if _planning_service is None:
        _planning_service = PlanningService(db)
    else:
        _planning_service.db = db
        _planning_service.tasks = db.tasks
        _planning_service.projects = db.projects
    return _planning_service


async def get_obsidian_writer(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """ObsidianWriter WITH a db handle.

    The handle is not optional in practice: it is what records the content hash
    of everything ARIA writes into the vault. Without it the VaultReader has no
    baseline to compare against and reads ARIA's own note back as one of Ben's
    edits — a self-triggering loop on the one surface that is supposed to carry
    his intent.
    """
    from aria.integrations.obsidian import ObsidianWriter

    return ObsidianWriter(db=db)


async def get_vault_reader(
    db: Annotated[AsyncIOMotorDatabase, Depends(get_db)],
):
    """Shared VaultReader so an on-demand poll and the worker see one state."""
    global _vault_reader
    from aria.integrations.vault_reader import VaultReader

    if _vault_reader is None:
        _vault_reader = VaultReader(db=db)
    else:
        _vault_reader.db = db
    return _vault_reader


async def require_admin(
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
):
    """Gate for actions the global API key must NOT authorise.

    The steward plan's key split (§7.3): anything running as `ben` — including
    an unsandboxed coding agent — can read API_KEY out of .env, so API_KEY
    cannot be what stands between an agent and an irreversible action. Merging
    to trunk, deactivating the killswitch or e-stop, repointing an agent,
    changing the LLM route, starting/stopping a model server and flipping a
    retrieval capability all move behind ADMIN_KEY, which is never placed in a
    session environment.

    Fails CLOSED: an unset ADMIN_KEY refuses rather than falling back to
    API_KEY. Silently accepting the global key when the admin key is
    unconfigured would make the whole split cosmetic.
    """
    import hmac

    from aria.config import settings

    if not settings.admin_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "ADMIN_KEY is not configured; admin actions are refused. "
                "Set ADMIN_KEY in .env (it is never placed in a session environment)."
            ),
        )
    if not hmac.compare_digest(x_admin_key or "", settings.admin_key):
        raise HTTPException(status_code=403, detail="Valid X-Admin-Key required")
    return True
