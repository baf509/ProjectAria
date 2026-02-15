import type { Conversation, HealthResponse, StreamChunk } from '@/types'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(`${API_URL}/api/v1${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
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

  async getConversation(id: string): Promise<Conversation> {
    const res = await apiFetch(`/conversations/${id}`)
    return res.json()
  },

  async createConversation(title = 'New Chat'): Promise<Conversation> {
    const res = await apiFetch('/conversations', {
      method: 'POST',
      body: JSON.stringify({ title }),
    })
    return res.json()
  },

  async *streamMessage(
    conversationId: string,
    content: string,
  ): AsyncGenerator<StreamChunk> {
    const res = await fetch(
      `${API_URL}/api/v1/conversations/${conversationId}/messages`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const data = line.slice(6).trim()
          if (!data || data === '[DONE]') continue
          try {
            const chunk: StreamChunk = JSON.parse(data)
            yield chunk
          } catch {
            // Skip malformed chunks
          }
        }
      }
    }
  },
}
