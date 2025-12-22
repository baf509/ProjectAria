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


class ConversationCreate(BaseModel):
    """Request to create a conversation."""
    agent_id: Optional[str] = None
    agent_slug: Optional[str] = None  # Alternative to agent_id
    title: Optional[str] = None


class ConversationUpdate(BaseModel):
    """Request to update a conversation."""
    title: Optional[str] = None
    status: Optional[str] = None
    tags: Optional[list[str]] = None
    pinned: Optional[bool] = None


class ConversationResponse(BaseModel):
    """Conversation response."""
    id: str
    agent_id: str
    title: str
    summary: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime
    llm_config: LLMConfig
    messages: list[Message] = []
    tags: list[str] = []
    pinned: bool = False
    stats: ConversationStats

    class Config:
        from_attributes = True


class ConversationListItem(BaseModel):
    """Conversation list item (without messages)."""
    id: str
    agent_id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    tags: list[str] = []
    pinned: bool = False
    stats: ConversationStats


# =============================================================================
# Agent Models
# =============================================================================

class AgentCapabilities(BaseModel):
    """Agent capabilities configuration."""
    memory_enabled: bool = True
    tools_enabled: bool = False
    computer_use_enabled: bool = False


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


class AgentCreate(BaseModel):
    """Request to create an agent."""
    name: str
    slug: str
    description: str
    system_prompt: str
    llm: AgentLLMConfig
    fallback_chain: Optional[list[FallbackLLM]] = []
    capabilities: Optional[AgentCapabilities] = None
    memory_config: Optional[MemoryConfig] = None
    enabled_tools: Optional[list[str]] = []


class AgentUpdate(BaseModel):
    """Request to update an agent."""
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    llm: Optional[AgentLLMConfig] = None
    fallback_chain: Optional[list[FallbackLLM]] = None
    capabilities: Optional[AgentCapabilities] = None
    memory_config: Optional[MemoryConfig] = None
    enabled_tools: Optional[list[str]] = None


class AgentResponse(BaseModel):
    """Agent response."""
    id: str
    name: str
    slug: str
    description: str
    system_prompt: str
    llm: AgentLLMConfig
    fallback_chain: list[FallbackLLM] = []
    capabilities: AgentCapabilities
    memory_config: MemoryConfig
    enabled_tools: list[str] = []
    is_default: bool = False
    created_at: datetime
    updated_at: datetime


# =============================================================================
# Health Models
# =============================================================================

class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    version: str
    database: str
    timestamp: datetime
