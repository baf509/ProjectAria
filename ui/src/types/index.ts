// ARIA TypeScript Types

export interface HealthResponse {
  status: string
  version: string
  database: string
  timestamp: string
}

export interface LLMStatus {
  backend: string
  available: boolean
  reason: string
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  tool_calls?: ToolCall[]
  model?: string
  tokens?: {
    input_tokens?: number
    output_tokens?: number
  }
  created_at: string
  memory_processed?: boolean
}

export interface ToolCall {
  id: string
  name: string
  arguments: Record<string, any>
  result?: any
  status?: string
  duration_ms?: number
}

export interface LLMConfig {
  backend: string
  model: string
  temperature: number
}

export interface ConversationStats {
  message_count: number
  total_tokens: number
  tool_calls: number
}

export interface Conversation {
  id: string
  agent_id: string
  title: string
  summary?: string
  status: string
  created_at: string
  updated_at: string
  llm_config: LLMConfig
  messages: Message[]
  tags: string[]
  pinned: boolean
  stats: ConversationStats
}

export interface ConversationListItem {
  id: string
  agent_id: string
  title: string
  status: string
  created_at: string
  updated_at: string
  tags: string[]
  pinned: boolean
  stats: ConversationStats
}

export interface AgentCapabilities {
  memory_enabled: boolean
  tools_enabled: boolean
  computer_use_enabled: boolean
}

export interface MemoryConfig {
  auto_extract: boolean
  short_term_messages: number
  long_term_results: number
  categories_filter?: string[]
}

export interface FallbackConditions {
  on_error: boolean
  on_context_overflow: boolean
  max_input_tokens?: number
}

export interface FallbackLLM {
  backend: string
  model: string
  conditions: FallbackConditions
}

export interface Agent {
  id: string
  name: string
  slug: string
  description: string
  system_prompt: string
  llm: LLMConfig
  fallback_chain: FallbackLLM[]
  capabilities: AgentCapabilities
  memory_config: MemoryConfig
  enabled_tools: string[]
  is_default: boolean
  created_at: string
  updated_at: string
}

export interface Memory {
  id: string
  content: string
  content_type: string
  importance: number
  categories: string[]
  source_type: string
  source_id?: string
  embedding?: number[]
  metadata: Record<string, any>
  access_count: number
  last_accessed?: string
  created_at: string
  updated_at: string
}

export interface ToolDefinition {
  name: string
  description: string
  type: string
  parameters: ToolParameter[]
}

export interface ToolParameter {
  name: string
  type: string
  description: string
  required: boolean
  default?: any
  enum?: any[]
}

export interface MCPServer {
  id: string
  name?: string
  version?: string
  connected: boolean
  command: string
  tool_count: number
}

// Streaming response types
export interface StreamChunk {
  type: 'text' | 'tool_call' | 'done' | 'error'
  content?: string
  tool_call?: ToolCall
  usage?: {
    input_tokens: number
    output_tokens: number
  }
  error?: string
}
