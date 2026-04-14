"""
ARIA - API Dependencies

Phase: 1, 3
Purpose: Dependency injection for FastAPI routes

Related Spec Sections:
- Section 9.4: Dependency Injection
"""

from typing import Annotated
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from aria.db.mongodb import get_database
from aria.core.orchestrator import Orchestrator
from aria.infrastructure.model_switcher import LlamaCppModelSwitcher
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
from aria.telegram.bot import TelegramBot
from aria.telegram.handler import TelegramHandler
from aria.heartbeat.service import HeartbeatService
from aria.awareness.service import AwarenessService
from aria.dreams.service import DreamService
from aria.agents.estop import EstopManager, RateLimitWatchdog
from aria.agents.mail import AgentMailbox
from aria.notifications.escalation import EscalationManager

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
_model_switcher: LlamaCppModelSwitcher = None
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
_telegram_bot: TelegramBot = None
_telegram_handler: TelegramHandler = None
_heartbeat_service: HeartbeatService = None
_dream_service: DreamService = None
_awareness_service: AwarenessService = None
_estop_manager: EstopManager = None
_rate_limit_watchdog: RateLimitWatchdog = None
_agent_mailbox: AgentMailbox = None
_escalation_manager: EscalationManager = None


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


def get_model_switcher() -> LlamaCppModelSwitcher:
    """Get shared llama.cpp model switcher."""
    global _model_switcher
    if _model_switcher is None:
        _model_switcher = LlamaCppModelSwitcher()
    return _model_switcher


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


def get_telegram_handler() -> TelegramHandler:
    """Get Telegram handler singleton."""
    global _telegram_handler, _telegram_bot
    if _telegram_handler is None:
        if _telegram_bot is None:
            _telegram_bot = TelegramBot()
        _telegram_handler = TelegramHandler(_telegram_bot)
    return _telegram_handler


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
    global _rate_limit_watchdog, _estop_manager
    if _estop_manager is None:
        _estop_manager = EstopManager(db)
    if _rate_limit_watchdog is None:
        _rate_limit_watchdog = RateLimitWatchdog(
            db, _estop_manager, get_notification_service()
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
