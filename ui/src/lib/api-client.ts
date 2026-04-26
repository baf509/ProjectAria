import type { Agent, Conversation, HealthResponse, Memory, PlanningProject, PlanningTask, ResearchRun, StreamChunk, UsageSummary, Workflow } from '@/types'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || ''

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(`${API_URL}/api/v1${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(API_KEY ? { 'X-API-Key': API_KEY } : {}),
      ...init?.headers,
    },
  })
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${res.statusText}`)
  }
  return res
}

export const apiClient = {
  async checkHealth(): Promise<HealthResponse> {
    const res = await apiFetch('/health')
    return res.json()
  },

  async listConversations(limit = 20): Promise<Conversation[]> {
    const res = await apiFetch(`/conversations?limit=${limit}`)
    return res.json()
  },

  async searchConversations(query: string, limit = 20): Promise<Conversation[]> {
    const res = await apiFetch(`/conversations?limit=${limit}&q=${encodeURIComponent(query)}`)
    return res.json()
  },

  async getConversation(id: string): Promise<Conversation> {
    const res = await apiFetch(`/conversations/${id}`)
    return res.json()
  },

  async deleteConversation(id: string): Promise<void> {
    await apiFetch(`/conversations/${id}`, { method: 'DELETE' })
  },

  async exportConversation(id: string, format: 'json' | 'markdown' = 'markdown'): Promise<any> {
    const res = await apiFetch(`/conversations/${id}/export?format=${format}`)
    return res.json()
  },

  async createConversation(title = 'New Chat'): Promise<Conversation> {
    const res = await apiFetch('/conversations', {
      method: 'POST',
      body: JSON.stringify({ title }),
    })
    return res.json()
  },

  async listAgents(): Promise<Agent[]> {
    const res = await apiFetch('/agents')
    return res.json()
  },

  async createAgent(body: Record<string, any>): Promise<Agent> {
    const res = await apiFetch('/agents', {
      method: 'POST',
      body: JSON.stringify(body),
    })
    return res.json()
  },

  async updateAgent(agentId: string, updates: Partial<Agent>): Promise<Agent> {
    const res = await apiFetch(`/agents/${agentId}`, {
      method: 'PUT',
      body: JSON.stringify(updates),
    })
    return res.json()
  },

  async deleteAgent(agentId: string): Promise<void> {
    await apiFetch(`/agents/${agentId}`, { method: 'DELETE' })
  },

  async switchConversationMode(conversationId: string, agentSlug: string): Promise<Conversation> {
    const res = await apiFetch(`/conversations/${conversationId}/switch-mode`, {
      method: 'POST',
      body: JSON.stringify({ agent_slug: agentSlug }),
    })
    return res.json()
  },

  async listMemories(limit = 100): Promise<Memory[]> {
    const res = await apiFetch(`/memories?limit=${limit}`)
    return res.json()
  },

  async deleteMemory(memoryId: string): Promise<void> {
    await apiFetch(`/memories/${memoryId}`, { method: 'DELETE' })
  },

  async searchMemories(query: string, limit = 20): Promise<Memory[]> {
    const res = await apiFetch('/memories/search', {
      method: 'POST',
      body: JSON.stringify({ query, limit }),
    })
    return res.json()
  },

  async exportMemories(format: 'json' | 'markdown' = 'json'): Promise<any> {
    const res = await apiFetch(`/memories/export?format=${format}`)
    return res.json()
  },

  async listResearchRuns(): Promise<ResearchRun[]> {
    const res = await apiFetch('/research')
    return res.json()
  },

  async getResearchReport(id: string): Promise<any> {
    const res = await apiFetch(`/research/${id}/report`)
    return res.json()
  },

  async usageSummary(days = 7): Promise<UsageSummary> {
    const res = await apiFetch(`/usage/summary?days=${days}`)
    return res.json()
  },

  async usageByAgent(days = 7): Promise<any[]> {
    const res = await apiFetch(`/usage/by-agent?days=${days}`)
    return res.json()
  },

  async usageByModel(days = 7): Promise<any[]> {
    const res = await apiFetch(`/usage/by-model?days=${days}`)
    return res.json()
  },

  async listTasks(status?: string): Promise<any[]> {
    const res = await apiFetch(`/tasks${status ? `?status=${encodeURIComponent(status)}` : ''}`)
    return res.json()
  },

  // -------------------------------------------------- Planning (todos + projects)
  async listTodos(status?: string, projectId?: string): Promise<PlanningTask[]> {
    const params = new URLSearchParams()
    if (status) params.set('status', status)
    if (projectId) params.set('project_id', projectId)
    const qs = params.toString()
    const res = await apiFetch(`/todos${qs ? `?${qs}` : ''}`)
    const body = await res.json()
    return body.tasks || []
  },

  async createTodo(body: { title: string; notes?: string; project_id?: string; due_at?: string }): Promise<PlanningTask> {
    const res = await apiFetch('/todos', {
      method: 'POST',
      body: JSON.stringify(body),
    })
    return res.json()
  },

  async acceptTodo(taskId: string): Promise<PlanningTask> {
    const res = await apiFetch(`/todos/${taskId}/accept`, { method: 'POST' })
    return res.json()
  },

  async dismissTodo(taskId: string): Promise<PlanningTask> {
    const res = await apiFetch(`/todos/${taskId}/dismiss`, { method: 'POST' })
    return res.json()
  },

  async completeTodo(taskId: string): Promise<PlanningTask> {
    const res = await apiFetch(`/todos/${taskId}/done`, { method: 'POST' })
    return res.json()
  },

  async deleteTodo(taskId: string): Promise<void> {
    await apiFetch(`/todos/${taskId}`, { method: 'DELETE' })
  },

  async listPlanningProjects(): Promise<PlanningProject[]> {
    const res = await apiFetch('/projects')
    const body = await res.json()
    return body.projects || []
  },

  async createPlanningProject(body: { name: string; slug?: string; summary?: string }): Promise<PlanningProject> {
    const res = await apiFetch('/projects', {
      method: 'POST',
      body: JSON.stringify(body),
    })
    return res.json()
  },

  async deletePlanningProject(projectId: string): Promise<void> {
    await apiFetch(`/projects/${projectId}`, { method: 'DELETE' })
  },

  async listWorkflows(): Promise<Workflow[]> {
    const res = await apiFetch('/workflows')
    return res.json()
  },

  async createWorkflow(body: {
    name: string
    description?: string
    tags?: string[]
    steps: Array<{ action: string; params: Record<string, any>; depends_on?: number[] }>
  }): Promise<any> {
    const res = await apiFetch('/workflows', {
      method: 'POST',
      body: JSON.stringify(body),
    })
    return res.json()
  },

  async deleteWorkflow(workflowId: string): Promise<void> {
    await apiFetch(`/workflows/${workflowId}`, { method: 'DELETE' })
  },

  async runWorkflow(workflowId: string, dryRun = false): Promise<any> {
    const res = await apiFetch(`/workflows/${workflowId}/run`, {
      method: 'POST',
      body: JSON.stringify({ dry_run: dryRun }),
    })
    return res.json()
  },

  async workflowStatus(workflowId: string): Promise<any> {
    const res = await apiFetch(`/workflows/${workflowId}/status`)
    return res.json()
  },

  async auditOverview(hours = 24, limit = 50): Promise<any> {
    const res = await apiFetch(`/admin/audit?hours=${hours}&limit=${limit}`)
    return res.json()
  },

  async cutoverStatus(): Promise<any> {
    const res = await apiFetch('/admin/cutover')
    return res.json()
  },

  async listInfrastructureModels(): Promise<any> {
    const res = await apiFetch('/infrastructure/llamacpp/models')
    return res.json()
  },

  async *streamMessage(
    conversationId: string,
    content: string,
    lastEventId?: string,
  ): AsyncGenerator<StreamChunk> {
    const res = await fetch(
      `${API_URL}/api/v1/conversations/${conversationId}/messages`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(API_KEY ? { 'X-API-Key': API_KEY } : {}),
          ...(lastEventId ? { 'Last-Event-ID': lastEventId } : {}),
        },
        body: JSON.stringify({ content, stream: true }),
      },
    )

    if (!res.ok) {
      yield { type: 'error', error: `API error ${res.status}: ${res.statusText}` }
      return
    }

    const reader = res.body?.getReader()
    if (!reader) {
      yield { type: 'error', error: 'No response body' }
      return
    }

    const decoder = new TextDecoder()
    let buffer = ''
    let currentEvent: { id?: string; event?: string; data: string[] } = { data: [] }

    const flushEvent = async function* (): AsyncGenerator<StreamChunk> {
      if (currentEvent.data.length === 0) {
        return
      }

      const eventMeta = currentEvent
      const rawData = eventMeta.data.join('\n').trim()
      currentEvent = { data: [] }

      if (!rawData || rawData === '[DONE]') {
        return
      }

      try {
        const parsed = JSON.parse(rawData) as StreamChunk
        if (eventMeta.event && !parsed.type) {
          parsed.type = eventMeta.event as StreamChunk['type']
        }
        if (eventMeta.id) {
          parsed.event_id = eventMeta.id
        }
        yield parsed
      } catch {
        yield { type: 'error', error: 'Malformed SSE payload' }
      }
    }

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split(/\r?\n/)
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line === '') {
            for await (const chunk of flushEvent()) {
              yield chunk
            }
            continue
          }
          if (line.startsWith(':')) {
            continue
          }
          if (line.startsWith('id:')) {
            currentEvent.id = line.slice(3).trim()
            continue
          }
          if (line.startsWith('event:')) {
            currentEvent.event = line.slice(6).trim()
            continue
          }
          if (line.startsWith('data:')) {
            currentEvent.data.push(line.slice(5).trimStart())
          }
        }
      }

      for await (const chunk of flushEvent()) {
        yield chunk
      }
    } finally {
      reader.releaseLock()
    }
  },
}
