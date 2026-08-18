/**
 * ARIA - endpoint paths and typed callers.
 *
 * The path IS the SWR key, so two routes asking for the same data share one
 * request and one cache entry (the 73KB fleet payload used to be fetched
 * independently by three pages).
 */
import { api } from '@/lib/http'
import type {
  Alert,
  Agent,
  Conversation,
  DecisionValue,
  LlmRoute,
  ModelServersResponse,
  ProjectsOverview,
  ReviewItem,
  ServiceEntry,
  ShellOverview,
  ShellsResponse,
  SoulProposal,
  Todo,
  Utilization,
} from './types'

/* ---------------------------------------------------------------- keys ---- */

export const K = {
  health: '/health',
  alerts: (needsHuman?: boolean, limit = 200) =>
    `/alerts?${needsHuman !== undefined ? `needs_human=${needsHuman}&` : ''}unacked_only=true&limit=${limit}`,
  todos: '/todos?status=proposed',
  soulProposals: '/dreams/soul-proposals',
  review: (limit = 200) => `/shared/review?limit=${limit}`,
  /**
   * The fleet, list view: only the fields a list renders. The full row is 78 KB
   * across 27 servers and ~84% of that is registry prose a list never shows —
   * re-sent on every poll, to a phone, over the tailnet. 13 KB here.
   */
  modelServers: '/infrastructure/model-servers?view=list',
  /** The full row for ONE server, for the detail view. */
  modelServer: (slug: string) => `/infrastructure/model-servers/${encodeURIComponent(slug)}`,
  services: '/infrastructure/services',
  llmRoute: '/infrastructure/llm-route',
  utilization: '/infrastructure/model-servers/utilization',
  // Per-pool memory. The old UI drew ONE bar from gtt_used/gtt_total, so the
  // discrete R9700's 32 GiB was invisible and the Halo bar looked like plain
  // system memory with no explanation. This endpoint returns both cards and all
  // three pools.
  devices: '/infrastructure/model-servers/devices',
  projectsOverview: '/projects/overview',
  projectCockpit: (slug: string) => `/projects/${slug}/cockpit`,
  shells: (status = 'active,idle') => `/shells?status=${encodeURIComponent(status)}`,
  shellsAll: '/shells',
  shellsOverview: '/shells/overview',
  shell: (name: string) => `/shells/${encodeURIComponent(name)}`,
  shellScreen: (name: string) => `/shells/${encodeURIComponent(name)}/screen`,
  shellEvents: (name: string, sinceLine: number, limit = 400) =>
    `/shells/${encodeURIComponent(name)}/events?since_line=${sinceLine}&limit=${limit}`,
  shellStream: (name: string, sinceLine: number) =>
    `/shells/${encodeURIComponent(name)}/stream?since_line=${sinceLine}`,
  // `view=list` omits system_prompt (62% of the payload); no list surface
  // renders it. The agent editor would need the full row.
  agents: '/agents?view=list',
  conversations: (limit = 50) => `/conversations?limit=${limit}`,
  conversation: (id: string) => `/conversations/${id}`,
  memories: (limit = 50) => `/memories?limit=${limit}`,
  research: '/research',
  workflows: '/workflows',
  tasks: '/tasks',
  usage: (days = 7) => `/usage/summary?days=${days}`,
  usageByModel: (days = 7) => `/usage/by-model?days=${days}`,
  benchRuns: (limit = 25) => `/benchmarks/runs?limit=${limit}`,
  benchSuites: '/benchmarks/suites',
  benchTargets: '/benchmarks/targets',
  benchHealth: '/benchmarks/health',
  dreamsStatus: '/dreams/status',
  awarenessStatus: '/awareness/status',
  heartbeatStatus: '/heartbeat/status',
  /**
   * `view=list` returns connections/knowledge_gaps/soul_proposals as COUNTS.
   * The page renders `.length` of each; shipping the arrays to count them was
   * 45 of the endpoint's 77 KB.
   */
  dreamsJournal: (limit = 10) => `/dreams/journal?limit=${limit}&view=list`,
  /**
   * 15, not 30: each observation carries a raw sensor `detail` averaging 2.5 KB,
   * so the old default shipped 82 KB to a phone for a panel that is collapsed
   * by default.
   */
  observations: (limit = 15) => `/awareness/observations?limit=${limit}`,
  capabilities: '/capabilities/retrieval',
  benchRun: (id: string, tail = 120) => `/benchmarks/runs/${encodeURIComponent(id)}?tail=${tail}`,
  // --- Know (/know/* split, 2026-08-17) ---
  // Browse newest-first with real server paging: the old dashboard filtered
  // the newest 50 of ~14,000 client-side, which made older memories look gone.
  memoriesPage: (limit = 50, skip = 0) => `/memories?limit=${limit}&skip=${skip}`,
  todosPlanning: '/todos?status=proposed,active',
  // `view=list` omits the harvester's `sources` provenance (57% of the
  // payload), which nothing renders.
  planningProjects: '/projects?view=list',
  workflowStatus: (id: string) => `/workflows/${encodeURIComponent(id)}/status`,
  usageByAgent: (days = 7) => `/usage/by-agent?days=${days}`,
  // --- Shells (supervise refit, 2026-08-17) ---
  // Stored snapshot: the only pane view that survives a stopped shell
  // (`/screen` 409s the moment tmux is gone).
  shellSnapshot: (name: string) => `/shells/${encodeURIComponent(name)}/snapshot`,
} as const

/* ------------------------------------------------------------- mutations -- */

export const ackAlert = (id: string) => api(`/alerts/${id}/ack`, { method: 'POST' })

/**
 * Records Ben's typed decision. NOTE: the API writes `decision` and clears
 * `needs_human`; nothing consumes `decision.value === 'APPLY'` yet, so the UI
 * says "recorded" rather than implying the fix was applied.
 */
export const decideAlert = (id: string, action: DecisionValue, note?: string) =>
  api(`/alerts/${id}/decide`, { method: 'POST', body: { action, note } })

export const acceptTodo = (id: string) => api(`/todos/${id}/accept`, { method: 'POST' })
export const dismissTodo = (id: string) => api(`/todos/${id}/dismiss`, { method: 'POST' })
export const ackReview = (id: string) => api(`/shared/review/${id}/ack`, { method: 'POST' })
export const approveProposal = (id: string, force = false) =>
  api(`/dreams/soul-proposals/${id}/approve${force ? '?force=true' : ''}`, { method: 'POST' })
export const rejectProposal = (id: string) =>
  api(`/dreams/soul-proposals/${id}/reject`, { method: 'POST' })

export const startModelServer = (slug: string, force = false) =>
  api(`/infrastructure/model-servers/${slug}/start${force ? '?force=true' : ''}`, { method: 'POST' })
export const stopModelServer = (slug: string) =>
  api(`/infrastructure/model-servers/${slug}/stop`, { method: 'POST' })
export const setLlmRoute = (pinned: string | null) =>
  api<LlmRoute>('/infrastructure/llm-route', { method: 'PUT', body: { pinned } })
export const startService = (slug: string) =>
  api(`/infrastructure/services/${slug}/start`, { method: 'POST' })
export const stopService = (slug: string) =>
  api(`/infrastructure/services/${slug}/stop`, { method: 'POST' })
export const sendShellInput = (name: string, text: string, waitMs = 400) =>
  api(`/shells/${encodeURIComponent(name)}/input`, { method: 'POST', body: { text, wait_ms: waitMs } })

/* --------------------------------------------------------------- helpers -- */

export type { Alert, Agent, Conversation, LlmRoute, ModelServersResponse, ProjectsOverview, ReviewItem, ServiceEntry, ShellOverview, ShellsResponse, SoulProposal, Todo, Utilization }

/* ---------------------------------------------- autonomy + benchmarks ----- */

import type { BenchRun, StartBenchRunBody } from './types'

export const triggerDream = () => api('/dreams/trigger', { method: 'POST' })
export const pollAwareness = () => api('/awareness/poll', { method: 'POST' })
export const analyzeAwareness = () => api('/awareness/analyze', { method: 'POST' })
export const triggerHeartbeat = () => api('/heartbeat/trigger', { method: 'POST' })

/**
 * Starting a run stops and starts model servers, so it is never optimistic;
 * a 409 carries {error, conflicts} (bound servers the run would disturb) and
 * the caller may retry with force=true.
 */
export const startBenchRun = (body: StartBenchRunBody) =>
  api<BenchRun>('/benchmarks/runs', { method: 'POST', body })
export const cancelBenchRun = (id: string) =>
  api<BenchRun>(`/benchmarks/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' })

/**
 * Drop a finished run from the list. The results directory and logs on disk are
 * untouched — this is the index, not the measurement. Added 2026-08-17: ten
 * SIGTERMed runs from 2026-08-07/08 were still the entire contents of this
 * screen with no way to clear them from any surface.
 */
export const dismissBenchRun = (id: string) =>
  api<{ run_id: string; dismissed: boolean }>(`/benchmarks/runs/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })

/** Bulk form: a model server vanishing mid-sweep fails every target at once. */
export const dismissFinishedBenchRuns = (keep = 0) =>
  api<{ count: number; kept: number }>(`/benchmarks/runs/dismiss-finished?keep=${keep}`, {
    method: 'POST',
  })

/* ------------------------------------------------------------- converse -- */

import type { ConversationDetail } from './types'

/** Server-side title/summary/content search (regex, case-insensitive). */
export const conversationSearchKey = (q: string, limit = 50) =>
  `/conversations?limit=${limit}&q=${encodeURIComponent(q)}`

/**
 * `agent_slug` is REQUIRED in practice: the default `aria` agent is
 * deliberately disabled, so a create without a slug resolves to it and is
 * refused with a 400 — which the old chat page swallowed, making "+ New Chat"
 * appear to do nothing. Callers pick an `enabled !== false` agent first.
 */
export const createConversation = (body: { title?: string; agent_slug: string; private?: boolean }) =>
  api<ConversationDetail>('/conversations', { method: 'POST', body })

export const deleteConversation = (id: string) =>
  api<void>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' })

export const switchConversationMode = (id: string, agentSlug: string) =>
  api<ConversationDetail>(`/conversations/${encodeURIComponent(id)}/switch-mode`, {
    method: 'POST',
    body: { agent_slug: agentSlug },
  })

/* ------------------------------------------------- know (/know/* split) --- */

import type { Memory as MemoryItem } from './types'

/**
 * SERVER-side memory search (POST — there is no GET variant). The endpoint has
 * no offset, so "more results" raises `limit` rather than paging.
 */
export const searchMemories = (query: string, limit = 20) =>
  api<MemoryItem[]>('/memories/search', { method: 'POST', body: { query, limit } })
export const deleteMemory = (id: string) => api(`/memories/${id}`, { method: 'DELETE' })

export const createTodo = (title: string) => api('/todos', { method: 'POST', body: { title } })
export const completeTodo = (id: string) => api(`/todos/${id}/done`, { method: 'POST' })
export const deleteTodo = (id: string) => api(`/todos/${id}`, { method: 'DELETE' })
export const createPlanningProject = (name: string) =>
  api('/projects', { method: 'POST', body: { name } })

export const runWorkflow = (id: string, dryRun = false) =>
  api(`/workflows/${encodeURIComponent(id)}/run`, { method: 'POST', body: { dry_run: dryRun } })
export const deleteWorkflow = (id: string) =>
  api(`/workflows/${encodeURIComponent(id)}`, { method: 'DELETE' })
export const createWorkflow = (body: { name: string; description?: string; steps: unknown[] }) =>
  api('/workflows', { method: 'POST', body })

/**
 * Descriptive pairing only (NOT admin-gated): it records which server an agent
 * runs on; it does not change the agent's llm.backend/model routing.
 */
export const bindModelServer = (slug: string, agent: string, force = false) =>
  api(`/infrastructure/model-servers/${encodeURIComponent(slug)}/bind`, {
    method: 'POST',
    body: { agent, force },
  })
export const unbindModelServer = (agent: string) =>
  api('/infrastructure/model-servers/unbind', { method: 'POST', body: { agent } })

/* -------------------------------------------- operate: long-running actions */
/*
 * Appended for the /operate rebuild. These exist beside startModelServer /
 * stopModelServer above because loading or unloading a 100 GiB model can
 * outlive the transport's 20s default timeout, and because a start may carry
 * launch OVERRIDES in the body (omitting them is meaningful: the API clears
 * any override a previous start applied, so a plain start always means the
 * deployment's own defaults). The 409 refusals here carry the useful part
 * ("preflight rejected: MemAvailable …") in FastAPI's `detail`, which the
 * transport preserves.
 */
export const modelServerAction = (
  slug: string,
  action: 'start' | 'stop' | 'sleep',
  body?: { force?: boolean; overrides?: Record<string, string> }
) =>
  api(`/infrastructure/model-servers/${encodeURIComponent(slug)}/${action}`, {
    method: 'POST',
    body,
    timeoutMs: 180_000,
  })

/** docker compose up of a cold container routinely exceeds 20s. */
export const serviceAction = (slug: string, action: 'start' | 'stop') =>
  api(`/infrastructure/services/${encodeURIComponent(slug)}/${action}`, {
    method: 'POST',
    timeoutMs: 120_000,
  })

/* ------------------------------------------------- shells (supervise) ----- */
import type { CodingSessionRequest, ShellInputResult } from './types'

/**
 * Shell input with full tmux semantics. The older `sendShellInput` above can
 * only send text+Enter; special keys (Esc, C-c, arrows) need
 * `append_enter=false`, and `wait_ms > 0` makes the response carry the
 * resulting pane so screen mode can act-and-observe instead of waiting a poll.
 */
export const sendShellKeys = (
  name: string,
  text: string,
  opts: { appendEnter?: boolean; literal?: boolean; waitMs?: number } = {}
) =>
  api<ShellInputResult>(`/shells/${encodeURIComponent(name)}/input`, {
    method: 'POST',
    body: {
      text,
      append_enter: opts.appendEnter ?? true,
      literal: opts.literal ?? false,
      wait_ms: opts.waitMs ?? 0,
    },
  })

/** Resize the tmux window so a TUI repaints at the phone's real geometry. */
export const resizeShell = (name: string, cols: number, rows: number) =>
  api(`/shells/${encodeURIComponent(name)}/resize`, { method: 'POST', body: { cols, rows } })

/**
 * Start a real coding session on the watched-shell substrate. Worktree
 * provisioning can take tens of seconds, so the timeout is raised above the
 * transport default — a 20s abort here reported failure for sessions that
 * then appeared anyway.
 */
export const createCodingSession = (body: CodingSessionRequest) =>
  api('/coding/sessions', { method: 'POST', body, timeoutMs: 90_000 })

/* ------------------------------------------------------- project lifecycle */

export type RetireReport = {
  project: string
  name: string
  dry_run: boolean
  shells?: string[]
  sessions?: number
  transcript_chars?: number
  record?: string
  memories_written?: string[]
  memories_verified?: number
  deleted?: boolean
  extraction_error?: string
}

/**
 * Retire a project: transcripts distilled into memory, then the row removed.
 * `dryRun` runs the identical server-side path without writing or deleting, so
 * the preview is the real thing rather than a client-side guess.
 */
export const retireProject = (slug: string, dryRun: boolean) =>
  api<RetireReport>(`/projects/${encodeURIComponent(slug)}/retire`, {
    method: 'POST',
    body: { dry_run: dryRun },
    // Extraction runs an LLM over the transcripts; the default 20s is not enough.
    timeoutMs: 180_000,
  })
