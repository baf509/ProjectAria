/**
 * Client for the modules that had no UI at all before the redesign:
 * dreams, awareness, heartbeat, alerts, shared review, and the scattered
 * approval actions that the Inbox unifies.
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || ''

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}/api/v1${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(API_KEY ? { 'X-API-Key': API_KEY } : {}),
      ...init?.headers,
    },
  })
  const body = await res.json().catch(() => null)
  if (!res.ok) throw new Error((body as any)?.detail || `API error ${res.status}`)
  return body as T
}

/* ------------------------------------------------------------------ dreams */

export interface JournalEntry {
  id: string
  journal_entry: string
  connections: unknown[]
  knowledge_gaps: unknown[]
  soul_proposals: unknown[]
  memory_consolidations_proposed: number
  created_at: string
}

export interface SoulProposal {
  id: string
  proposals: unknown[]
  status: string
  created_at: string
  reviewed_at?: string | null
  /** The text this proposal edits is no longer in SOUL.md — needs re-review. */
  stale?: boolean
  stale_sections?: string[]
}

export interface DreamStatus {
  enabled: boolean
  running: boolean
  interval_hours: number
  active_hours: Record<string, unknown>
  claude_model: string
  last_run?: string | null
  last_status?: string | null
  is_active_hours: boolean
}

/* --------------------------------------------------------------- awareness */

export interface Observation {
  sensor: string
  category: string
  event_type: string
  summary: string
  detail?: string | null
  severity: string
  tags: string[]
  created_at: string
}

export interface AwarenessStatus {
  enabled: boolean
  running: boolean
  sensors: string[]
  poll_interval_seconds: number
  analysis_interval_minutes: number
  observation_ttl_hours: number
  watch_dirs: string[]
  last_poll?: string | null
  last_analysis?: string | null
}

export interface Alert {
  id: string
  [k: string]: unknown
}

export const autonomyApi = {
  dreamStatus: () => req<DreamStatus>('/dreams/status'),
  journal: (limit = 20) => req<JournalEntry[]>(`/dreams/journal?limit=${limit}`),
  soulProposals: () => req<SoulProposal[]>('/dreams/soul-proposals'),
  approveProposal: (id: string, force = false) =>
    req<unknown>(
      `/dreams/soul-proposals/${encodeURIComponent(id)}/approve${force ? '?force=true' : ''}`,
      { method: 'POST' },
    ),
  rejectProposal: (id: string) =>
    req<unknown>(`/dreams/soul-proposals/${encodeURIComponent(id)}/reject`, { method: 'POST' }),
  triggerDream: () => req<unknown>('/dreams/trigger', { method: 'POST' }),

  awarenessStatus: () => req<AwarenessStatus>('/awareness/status'),
  observations: (limit = 40) => req<Observation[]>(`/awareness/observations?limit=${limit}`),
  awarenessSummary: () => req<any>('/awareness/summary'),
  poll: () => req<unknown>('/awareness/poll', { method: 'POST' }),
  analyze: () => req<unknown>('/awareness/analyze', { method: 'POST' }),

  heartbeatStatus: () => req<any>('/heartbeat/status'),
  heartbeatTrigger: () => req<unknown>('/heartbeat/trigger', { method: 'POST' }),

  alerts: (unackedOnly = true) =>
    req<{ alerts: Alert[]; count: number }>(`/alerts?unacked_only=${unackedOnly}&limit=50`),
  ackAlert: (id: string) => req<unknown>(`/alerts/${encodeURIComponent(id)}/ack`, { method: 'POST' }),

  reviewQueue: () => req<any>('/shared/review'),
  ackReview: (id: string) =>
    req<unknown>(`/shared/review/${encodeURIComponent(id)}/ack`, { method: 'POST' }),
}
