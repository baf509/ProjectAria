/**
 * ARIA - HTTP transport (browser side)
 *
 * One place that talks to the API, through a SAME-ORIGIN proxy. Consequences:
 * no `NEXT_PUBLIC_API_KEY` in the bundle (it was in eight JS chunks), no CORS
 * preflight per URL, no `?api_key=` in the shells SSE URL, and the page can be
 * served over HTTPS so the service worker is not a silent no-op.
 *
 * `ApiError` preserves FastAPI's `detail`. The previous helper threw
 * `API error 404: Not Found` and discarded the body, which is why nine call
 * sites hand-rolled their own fetch.
 */

export class ApiError extends Error {
  status: number
  detail: unknown
  path: string

  constructor(status: number, detail: unknown, path: string) {
    const message =
      typeof detail === 'string'
        ? detail
        : detail && typeof detail === 'object' && 'message' in (detail as Record<string, unknown>)
          ? String((detail as Record<string, unknown>).message)
          : `Request failed (${status})`
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.path = path
  }

  /** True when retrying could plausibly help. */
  get retryable() {
    return this.status >= 500 || this.status === 408 || this.status === 429
  }
}

export type ApiInit = {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  signal?: AbortSignal
  /**
   * Default 20s. Deliberately longer than the measured 8.8s worst case on
   * `/infrastructure/model-servers` — a timeout shorter than the slowest real
   * endpoint turns a slow page into a broken one.
   */
  timeoutMs?: number
  headers?: Record<string, string>
  /** Override the proxy prefix (the build-info route lives at /api/build). */
  base?: string
}

/** Session-only admin key. Never localStorage: it gates irreversible routes. */
let adminKey: string | null = null
export function setAdminKey(key: string | null) {
  adminKey = key
}
export function hasAdminKey() {
  return adminKey !== null
}

function withTimeout(signal: AbortSignal | undefined, ms: number): AbortSignal {
  const timeout = AbortSignal.timeout(ms)
  if (!signal) return timeout
  // AbortSignal.any is available everywhere this app runs (Node 20+/Safari 17+).
  return typeof AbortSignal.any === 'function' ? AbortSignal.any([signal, timeout]) : signal
}

export async function api<T>(path: string, init: ApiInit = {}): Promise<T> {
  const { method = 'GET', body, signal, timeoutMs = 20_000, headers = {}, base = '/api/v1' } = init
  const url = base + path

  const res = await fetch(url, {
    method,
    signal: withTimeout(signal, timeoutMs),
    headers: {
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(adminKey ? { 'X-Admin-Key': adminKey } : {}),
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  const text = await res.text()
  let parsed: unknown = undefined
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      parsed = text
    }
  }

  if (!res.ok) {
    const detail =
      parsed && typeof parsed === 'object' && 'detail' in (parsed as Record<string, unknown>)
        ? (parsed as Record<string, unknown>).detail
        : parsed
    throw new ApiError(res.status, detail, path)
  }

  return parsed as T
}

/** SWR's fetcher: the key IS the path, so the cache is shared across routes. */
export const fetcher = <T,>(path: string) => api<T>(path)
