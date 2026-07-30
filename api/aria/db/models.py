"""
ARIA - Pydantic Models

Phase: 1
Purpose: Data models for API requests/responses

Related Spec Sections:
- Section 4: Data Models
- Section 9.5: Pydantic Models
"""

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field


# =============================================================================
# Conversation Models
# =============================================================================

class MessageRequest(BaseModel):
    """Request to send a message."""
    content: str
    stream: bool = True


class ToolCall(BaseModel):
    """Tool call within a message."""
    id: str
    name: str
    arguments: dict
    result: Optional[Any] = None
    status: Optional[str] = None
    duration_ms: Optional[int] = None


class Message(BaseModel):
    """Message within a conversation."""
    id: str
    role: str  # "user" | "assistant" | "system" | "tool"
    content: str
    tool_calls: Optional[list[ToolCall]] = None
    model: Optional[str] = None
    tokens: Optional[dict[str, int]] = None
    created_at: datetime
    memory_processed: bool = False


class LLMConfig(BaseModel):
    """LLM configuration."""
    backend: str
    model: str
    temperature: float = 0.7


class ConversationStats(BaseModel):
    """Conversation statistics."""
    message_count: int = 0
    total_tokens: int = 0
    tool_calls: int = 0


class ConversationBranch(BaseModel):
    """Request to branch a conversation at a specific message."""
    message_index: int = Field(..., ge=0, description="Index of the message to branch from")
    title: Optional[str] = None


class ConversationCreate(BaseModel):
    """Request to create a conversation."""
    agent_id: Optional[str] = None
    agent_slug: Optional[str] = None  # Alternative to agent_id
    title: Optional[str] = None
    private: bool = False  # Private conversations use local LLM and skip memory


class ConversationUpdate(BaseModel):
    """Request to update a conversation."""
    title: Optional[str] = None
    status: Optional[str] = None
    tags: Optional[list[str]] = None
    pinned: Optional[bool] = None
    active_agent_id: Optional[str] = None
    private: Optional[bool] = None


class SteeringMessageRequest(BaseModel):
    """Request to send a mid-execution steering message."""
    content: str
    priority: str = "normal"  # "normal" | "interrupt"


class ConversationSwitchMode(BaseModel):
    """Request to switch the active mode for a conversation."""
    agent_slug: str


class ConversationResponse(BaseModel):
    """Conversation response."""
    id: str
    agent_id: str
    active_agent_id: Optional[str] = None
    title: str
    summary: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime
    llm_config: LLMConfig
    messages: list[Message] = []
    tags: list[str] = []
    pinned: bool = False
    private: bool = False
    stats: ConversationStats

    class Config:
        from_attributes = True


class ConversationListItem(BaseModel):
    """Conversation list item (without messages)."""
    id: str
    agent_id: str
    active_agent_id: Optional[str] = None
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    tags: list[str] = []
    pinned: bool = False
    private: bool = False
    stats: ConversationStats


# =============================================================================
# Agent Models
# =============================================================================

class AgentCapabilities(BaseModel):
    """Agent capabilities configuration."""
    memory_enabled: bool = True
    tools_enabled: bool = False
    computer_use_enabled: bool = False


class ModeMetadata(BaseModel):
    """UI and routing metadata for an agent mode."""
    icon: Optional[str] = None
    color: Optional[str] = None
    keywords: list[str] = []
    keyboard_shortcut: Optional[str] = None


class MemoryConfig(BaseModel):
    """Memory configuration for agent."""
    auto_extract: bool = True
    short_term_messages: int = 20
    long_term_results: int = 10
    categories_filter: Optional[list[str]] = None


class FallbackConditions(BaseModel):
    """Conditions for fallback LLM."""
    on_error: bool = True
    on_context_overflow: bool = True
    max_input_tokens: Optional[int] = None


class FallbackLLM(BaseModel):
    """Fallback LLM configuration."""
    backend: str
    model: str
    conditions: FallbackConditions


class AgentLLMConfig(BaseModel):
    """Agent LLM configuration."""
    backend: str
    model: str
    temperature: float = 0.7
    max_tokens: int = 4096
    max_context_tokens: Optional[int] = None
    force_non_streaming: bool = False  # Force non-streaming mode for models with streaming issues


class AgentCreate(BaseModel):
    """Request to create an agent."""
    name: str
    slug: str
    description: str
    system_prompt: str
    mode_category: str = "chat"
    greeting: Optional[str] = None
    context_instructions: Optional[str] = None
    llm: AgentLLMConfig
    fallback_chain: Optional[list[FallbackLLM]] = []
    capabilities: Optional[AgentCapabilities] = None
    mode_metadata: Optional[ModeMetadata] = None
    memory_config: Optional[MemoryConfig] = None
    enabled_tools: Optional[list[str]] = []


class AgentUpdate(BaseModel):
    """Request to update an agent."""
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    mode_category: Optional[str] = None
    greeting: Optional[str] = None
    context_instructions: Optional[str] = None
    llm: Optional[AgentLLMConfig] = None
    fallback_chain: Optional[list[FallbackLLM]] = None
    capabilities: Optional[AgentCapabilities] = None
    mode_metadata: Optional[ModeMetadata] = None
    memory_config: Optional[MemoryConfig] = None
    enabled_tools: Optional[list[str]] = None
    enabled: Optional[bool] = None


class AgentResponse(BaseModel):
    """Agent response."""
    id: str
    name: str
    slug: str
    description: str
    system_prompt: str
    mode_category: str = "chat"
    greeting: Optional[str] = None
    context_instructions: Optional[str] = None
    llm: AgentLLMConfig
    fallback_chain: list[FallbackLLM] = []
    capabilities: AgentCapabilities
    mode_metadata: ModeMetadata | None = None
    memory_config: MemoryConfig
    enabled_tools: list[str] = []
    is_default: bool = False
    # Paused agents (e.g. temporarily taken off a shared backend to reduce
    # contention) stay in db.agents rather than being deleted, but are refused
    # at conversation-creation/mode-switch time. Defaults true so every
    # existing agent doc (which predates this field) behaves unchanged.
    enabled: bool = True
    created_at: datetime
    updated_at: datetime


# =============================================================================
# Research Models
# =============================================================================

class ResearchCreate(BaseModel):
    """Request to start a research run."""
    query: str
    depth: int = Field(default=2, ge=1, le=4)
    breadth: int = Field(default=3, ge=1, le=6)
    model: Optional[str] = None
    backend: Optional[str] = None
    conversation_id: Optional[str] = None


class Learning(BaseModel):
    """Single extracted research learning."""
    content: str
    source_url: Optional[str] = None
    confidence: float = 0.5
    depth_found: int = 0
    query_context: str


class ResearchProgress(BaseModel):
    """Research execution progress."""
    current_depth: int = 0
    max_depth: int = 0
    queries_completed: int = 0
    queries_total: int = 0
    learnings_count: int = 0


class ResearchResponse(BaseModel):
    """Research run metadata."""
    id: str
    query: str
    status: str
    task_id: Optional[str] = None
    backend: str
    model: str
    depth: int
    breadth: int
    progress: ResearchProgress
    report_text: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


# =============================================================================
# Coding Session Models
# =============================================================================

class CodingLoopConfig(BaseModel):
    """Ralph-loop config: nudge an idle session forward until it signals done.

    All fields are optional; unset fields fall back to the coding_loop_*
    defaults in settings when the loop is (re)enabled.
    """
    nudge_prompt: Optional[str] = None       # text sent on each nudge
    nudge_prompt_file: Optional[str] = None  # re-read fresh each nudge (steer live)
    idle_seconds: Optional[int] = None       # idle-at-prompt time before a nudge
    done_regex: Optional[str] = None         # match in output → loop is done
    max_nudges: Optional[int] = None         # hard cap on nudges
    deadline_minutes: Optional[int] = None   # wall-clock cap
    notify_every: Optional[int] = None       # notify every N nudges (0 = silent)


class CodingSessionCreate(BaseModel):
    """Request to start a coding session."""
    workspace: str
    prompt: str
    backend: Optional[str] = None
    llm: Optional[str] = None
    model: Optional[str] = None
    branch: Optional[str] = None
    loop: Optional[CodingLoopConfig] = None  # opt-in Ralph loop; absent = one-shot
    host: Optional[str] = None  # run on a remote node (aria-node id); None = this host
    subagent_profile: Optional[str] = None  # named db.agents specialist (backend/model + role)


class CodingSessionInput(BaseModel):
    """Input payload for an existing coding session."""
    text: str


class CodingSessionResponse(BaseModel):
    """Coding session metadata."""
    id: str
    backend: str
    # LLM-adapter name for pi-code sessions (llamacpp/agentic/ridge/...) --
    # distinguishes a Ridge-backed pi-code session from a local one; both
    # share backend="pi-code". None for claude_code/codex/pool.
    llm: Optional[str] = None
    model: Optional[str] = None
    workspace: str
    prompt: str
    branch: Optional[str] = None
    pid: Optional[int] = None
    visible: bool = False
    tmux_pane_id: Optional[str] = None
    shell_name: Optional[str] = None
    status: str
    host: Optional[str] = None   # remote node id, or None for this host
    loop_enabled: bool = False  # true while a Ralph loop is nudging this session
    # How the model was chosen: {tier, why, confidence, source, judge_model,
    # decided_at}. None when the caller pinned the model explicitly.
    routing: Optional[dict] = None
    # Why a session failed. session.py writes `error` on every failure path,
    # but it was never surfaced — so an MCP agent saw only status="failed" with
    # no reason and could not tell a bad workspace from a dead backend from an
    # e-stop. That opacity is what turned a one-line backend typo into a silent
    # wrong-agent fallback.
    error: Optional[str] = None
    # Set by pi-code / awaited sessions on completion; the payload workflow
    # fan-out consumes via wait_for_session().
    result_summary: Optional[str] = None
    # Coherence C1 verification-gate history: [{at, passed, tail}, ...]. Empty
    # when the gate is disabled, was skipped every time, or never ran.
    gate_runs: list[dict] = []
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


# =============================================================================
# Health Models
# =============================================================================

class HealthResponse(BaseModel):
    """Health check response."""
    status: str  # "healthy", "degraded", or "unhealthy"
    version: str
    database: str
    timestamp: datetime
    embeddings: str = "unknown"
    llm: str = "unknown"
