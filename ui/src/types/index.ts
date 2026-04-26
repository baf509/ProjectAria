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
  active_agent_id?: string | null
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

export interface Workflow {
  _id: string
  name: string
  description: string
  tags: string[]
  steps: Array<{
    action: string
    params: Record<string, any>
    depends_on: number[]
  }>
  created_at: string
  updated_at: string
}

export interface ConversationListItem {
  id: string
  agent_id: string
  active_agent_id?: string | null
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

export interface ModeMetadata {
  icon?: string | null
  color?: string | null
  keywords?: string[]
  keyboard_shortcut?: string | null
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
  mode_category?: string
  greeting?: string | null
  context_instructions?: string | null
  system_prompt: string
  llm: LLMConfig
  fallback_chain: FallbackLLM[]
  capabilities: AgentCapabilities
  mode_metadata?: ModeMetadata | null
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
  confidence?: number
  verified?: boolean
  source: Record<string, any>
  access_count: number
  created_at: string
}

export interface PlanningTaskSource {
  type: 'manual' | 'conversation' | 'shell' | 'awareness' | 'import'
  conversation_id?: string | null
  shell_name?: string | null
  message_ids?: string[] | null
  extracted_at?: string | null
  confidence?: number | null
}

export interface PlanningTask {
  id: string
  title: string
  notes?: string | null
  status: 'proposed' | 'active' | 'done' | 'dismissed'
  due_at?: string | null
  project_id?: string | null
  tags: string[]
  source: PlanningTaskSource
  content_hash: string
  created_at: string
  updated_at: string
  completed_at?: string | null
}

export interface PlanningProjectActivity {
  at: string
  source: string
  note: string
}

export interface PlanningProject {
  id: string
  name: string
  slug: string
  summary: string
  status: 'active' | 'paused' | 'archived'
  next_steps: string[]
  relevant_paths: string[]
  tags: string[]
  recent_activity: PlanningProjectActivity[]
  created_at: string
  updated_at: string
  last_signal_at?: string | null
}

export interface ResearchRun {
  id: string
  query: string
  status: string
  task_id?: string | null
  backend: string
  model: string
  depth: number
  breadth: number
  progress: {
    current_depth: number
    max_depth: number
    queries_completed: number
    queries_total: number
    learnings_count: number
  }
  report_text?: string | null
  created_at: string
  updated_at: string
  completed_at?: string | null
}

export interface UsageSummary {
  requests: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
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
  event_id?: string
  content?: string
  tool_call?: ToolCall
  usage?: {
    input_tokens: number
    output_tokens: number
  }
  error?: string
}
