// ARIA - Coherence C4 cockpit API client helpers.
//
// Uses the same base URL + API key conventions as @/lib/api-client.

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || ''

function headers(extra: Record<string, string> = {}): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    ...(API_KEY ? { 'X-API-Key': API_KEY } : {}),
    ...extra,
  }
}

export interface AttentionCounts {
  shells: number
  blocked_shells: number
  working_shells: number
  running_sessions: number
  gate_failed_sessions: number
  unacked_alerts: number
  open_tasks: number
  stale_tasks: number
}

export interface HarvestedGit {
  branch: string | null
  last_commit_at: string | null
  last_commit_subject: string | null
}

export interface LiveGit {
  branch: string | null
  dirty_files: number
}

export interface OverviewProject {
  id: string
  name: string
  slug: string
  summary: string | null
  status: string
  activity_status: string
  last_activity_at: string | null
  path: string | null
  git: HarvestedGit | null
  next_steps: string[]
  attention: AttentionCounts
  attention_score: number
}

export interface ProjectsOverview {
  projects: OverviewProject[]
  active_project: string | null
  unacked_alerts_total: number
  generated_at: string
}

export interface CockpitProject {
  id?: string
  name: string
  slug: string
  summary: string | null
  status: string
  next_steps: string[]
  check_command: string | null
  path: string | null
  [key: string]: unknown
}

export interface CockpitShell {
  name: string
  short_name: string
  status: string
  activity_state: 'working' | 'blocked' | 'done' | 'idle'
  host: string
  project_dir: string
  idle_seconds: number | null
  awaiting_input: boolean
  prompt_line: string | null
  last_line: string | null
  tags: string[]
}

export interface GateRun {
  at: string
  passed: boolean
  tail: string | null
}

export interface CockpitSession {
  id: string
  backend: string
  model: string | null
  status: string
  host: string | null
  shell_name: string | null
  workspace: string | null
  looping: boolean
  result_summary: string | null
  gate_runs: GateRun[]
  created_at: string
  updated_at: string
}

export interface CockpitTask {
  id: string
  title: string
  notes: string | null
  status: string
  project_id: string | null
  stale: boolean
  updated_at: string | null
  [key: string]: unknown
}

export interface CockpitAlert {
  id: string
  source: string
  event_type: string
  message: string
  created_at: string
}

export interface ChangedEntry {
  content: string
  created_at: string
}

export interface LinearItem {
  id: string
  title: string
  status: string
  external_ref: string | null
  proposed_disposition: string | null
  updated_at: string | null
}

export interface CockpitBudget {
  cost: number
  total_tokens: number
  sessions_priced: number
}

export interface ProjectCockpit {
  project: CockpitProject
  attention: AttentionCounts
  attention_score: number
  git: {
    harvested: HarvestedGit | null
    live: LiveGit | null
  }
  shells: CockpitShell[]
  sessions: CockpitSession[]
  tasks: CockpitTask[]
  alerts: CockpitAlert[]
  changed: ChangedEntry[]
  linear: LinearItem[]
  budget: CockpitBudget
  vault_folder: string | null
  generated_at: string
}

export const cockpitApi = {
  async getOverview(includeArchived = false): Promise<ProjectsOverview> {
    const qs = includeArchived ? '?include_archived=true' : ''
    const res = await fetch(`${API_URL}/api/v1/projects/overview${qs}`, { headers: headers() })
    if (!res.ok) throw new Error(`projects overview: ${res.status}`)
    return res.json()
  },

  async getCockpit(slug: string): Promise<ProjectCockpit> {
    const res = await fetch(`${API_URL}/api/v1/projects/${encodeURIComponent(slug)}/cockpit`, {
      headers: headers(),
    })
    if (!res.ok) throw new Error(`project cockpit: ${res.status}`)
    return res.json()
  },

  async getActive(): Promise<{ active_project: string | null }> {
    const res = await fetch(`${API_URL}/api/v1/projects/active`, { headers: headers() })
    if (!res.ok) throw new Error(`get active project: ${res.status}`)
    return res.json()
  },

  async setActive(slug: string | null): Promise<{ active_project: string | null }> {
    const res = await fetch(`${API_URL}/api/v1/projects/active`, {
      method: 'PUT',
      headers: headers(),
      body: JSON.stringify({ slug }),
    })
    if (!res.ok) throw new Error(`set active project: ${res.status}`)
    return res.json()
  },
}
