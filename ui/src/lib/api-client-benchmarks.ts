// ARIA - Benchmarks API client helpers.
//
// Wraps /api/v1/benchmarks (see api/aria/api/routes/benchmarks.py), which drives
// the evalstack harness. Same base URL + API key conventions as @/lib/api-client.

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || ''

function headers(extra: Record<string, string> = {}): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    ...(API_KEY ? { 'X-API-Key': API_KEY } : {}),
    ...extra,
  }
}

export interface SuiteBench {
  id: string
  what: string
  category: string
  flags: string[]
}

export interface Suite {
  name: string
  benches: SuiteBench[]
}

export interface BenchTarget {
  name: string
  model: string
  base_url: string
  vram_gb: number
  deployment: string
  cloud: boolean
  tags: string[]
}

export interface BenchMetric {
  target: string
  benchmark: string
  metric: string
  value: number
  n: number | null
}

export interface BenchRun {
  run_id: string
  pid: number | null
  status: 'running' | 'succeeded' | 'failed' | 'cancelled' | 'unknown'
  suites: string[]
  targets: string[]
  limit: number | null
  started_at: number
  finished_at: number | null
  returncode: number | null
  log: string
  results_dir: string
  log_tail?: string
  metrics?: BenchMetric[]
  summary?: { run: string; ok: boolean; results: Array<Record<string, unknown>> } | null
}

export interface StartRunBody {
  suites: string[]
  targets: string[]
  run_id?: string
  limit?: number
  allow_coresident?: boolean
  keep_up?: boolean
  force?: boolean
}

/** Thrown for 409 "would disturb a bound model server" so the UI can offer force. */
export class BoundConflictError extends Error {
  conflicts: string[]
  constructor(message: string, conflicts: string[]) {
    super(message)
    this.name = 'BoundConflictError'
    this.conflicts = conflicts
  }
}

async function jsonOrThrow(res: Response) {
  if (res.ok) return res.json()
  let detail: unknown = await res.text()
  try {
    detail = JSON.parse(detail as string).detail ?? detail
  } catch {
    /* plain text */
  }
  if (res.status === 409 && detail && typeof detail === 'object') {
    const d = detail as { error?: string; conflicts?: string[] }
    throw new BoundConflictError(d.error || 'bound model conflict', d.conflicts || [])
  }
  throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
}

export const benchmarksApi = {
  async health(): Promise<{ available: boolean; root: string; gpu_budget_gb?: number }> {
    return jsonOrThrow(await fetch(`${API_URL}/api/v1/benchmarks/health`, { headers: headers() }))
  },

  async suites(): Promise<Suite[]> {
    const d = await jsonOrThrow(await fetch(`${API_URL}/api/v1/benchmarks/suites`, { headers: headers() }))
    return d.suites
  },

  async targets(): Promise<{ targets: BenchTarget[]; gpu_budget_gb: number }> {
    return jsonOrThrow(await fetch(`${API_URL}/api/v1/benchmarks/targets`, { headers: headers() }))
  },

  async listRuns(limit = 25): Promise<BenchRun[]> {
    const d = await jsonOrThrow(
      await fetch(`${API_URL}/api/v1/benchmarks/runs?limit=${limit}`, { headers: headers() }),
    )
    return d.runs
  },

  async getRun(runId: string, tail = 80): Promise<BenchRun> {
    return jsonOrThrow(
      await fetch(`${API_URL}/api/v1/benchmarks/runs/${encodeURIComponent(runId)}?tail=${tail}`, {
        headers: headers(),
      }),
    )
  },

  async startRun(body: StartRunBody): Promise<BenchRun> {
    return jsonOrThrow(
      await fetch(`${API_URL}/api/v1/benchmarks/runs`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(body),
      }),
    )
  },

  async cancel(runId: string): Promise<BenchRun> {
    return jsonOrThrow(
      await fetch(`${API_URL}/api/v1/benchmarks/runs/${encodeURIComponent(runId)}/cancel`, {
        method: 'POST',
        headers: headers(),
      }),
    )
  },
}
