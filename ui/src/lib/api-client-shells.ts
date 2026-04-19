// ARIA - Watched Shells API client helpers.
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

export interface Shell {
  name: string
  short_name: string
  project_dir: string
  host: string
  status: 'active' | 'idle' | 'stopped' | 'unknown'
  created_at: string
  last_activity_at: string
  last_output_at: string | null
  last_input_at: string | null
  line_count: number
  tags: string[]
  metadata: Record<string, unknown>
}

export interface ShellEvent {
  shell_name: string
  ts: string
  line_number: number
  kind: 'output' | 'input' | 'system'
  text_raw: string
  text_clean: string
  source: string
  byte_offset: number | null
}

export const shellsApi = {
  async list(status?: string): Promise<Shell[]> {
    const qs = status ? `?status=${encodeURIComponent(status)}` : ''
    const res = await fetch(`${API_URL}/api/v1/shells${qs}`, { headers: headers() })
    if (!res.ok) throw new Error(`list shells: ${res.status}`)
    const data = await res.json()
    return data.shells || []
  },

  async get(name: string): Promise<Shell> {
    const res = await fetch(`${API_URL}/api/v1/shells/${encodeURIComponent(name)}`, {
      headers: headers(),
    })
    if (!res.ok) throw new Error(`get shell: ${res.status}`)
    return res.json()
  },

  async listEvents(name: string, opts: { sinceLine?: number; limit?: number } = {}): Promise<ShellEvent[]> {
    const params = new URLSearchParams()
    if (opts.sinceLine != null) params.set('since_line', String(opts.sinceLine))
    params.set('limit', String(opts.limit ?? 500))
    const res = await fetch(
      `${API_URL}/api/v1/shells/${encodeURIComponent(name)}/events?${params}`,
      { headers: headers() }
    )
    if (!res.ok) throw new Error(`list events: ${res.status}`)
    const data = await res.json()
    return data.events || []
  },

  async sendInput(
    name: string,
    text: string,
    opts: { appendEnter?: boolean; literal?: boolean } = {}
  ): Promise<{ ok: boolean; line_number: number }> {
    const res = await fetch(`${API_URL}/api/v1/shells/${encodeURIComponent(name)}/input`, {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({
        text,
        append_enter: opts.appendEnter ?? true,
        literal: opts.literal ?? false,
      }),
    })
    if (!res.ok) {
      const detail = await res.text()
      throw new Error(`send input: ${res.status} ${detail}`)
    }
    return res.json()
  },

  async getSnapshot(name: string): Promise<{ shell_name: string; ts: string; content: string } | null> {
    const res = await fetch(`${API_URL}/api/v1/shells/${encodeURIComponent(name)}/snapshot`, {
      headers: headers(),
    })
    if (res.status === 404) return null
    if (!res.ok) throw new Error(`get snapshot: ${res.status}`)
    return res.json()
  },

  async setTags(name: string, tags: string[]): Promise<Shell> {
    const res = await fetch(`${API_URL}/api/v1/shells/${encodeURIComponent(name)}/tags`, {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ tags }),
    })
    if (!res.ok) throw new Error(`set tags: ${res.status}`)
    return res.json()
  },

  streamUrl(name: string, sinceLine?: number): string {
    const qs = sinceLine != null ? `?since_line=${sinceLine}` : ''
    return `${API_URL}/api/v1/shells/${encodeURIComponent(name)}/stream${qs}`
  },
}
