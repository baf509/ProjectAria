/**
 * ARIA - same-origin BFF proxy
 *
 * The browser talks only to this origin; the API key lives in the server
 * process, not in the bundle. This removes, in one move:
 *   - `NEXT_PUBLIC_API_KEY` from eight JS chunks,
 *   - an OPTIONS preflight per distinct URL (custom X-API-Key header),
 *   - `?api_key=<master key>` in the shells EventSource URL,
 *   - the mixed-content wall that made HTTPS (and therefore the service worker,
 *     and therefore the PWA) impossible.
 *
 * Streaming is passed through untouched: `upstream.body` is handed straight to
 * the Response, `no-transform` stops Next's compression from buffering it, and
 * the client's abort signal is forwarded so an abandoned /stream does not leave
 * the API polling Mongo every 0.5s forever.
 */
import { NextRequest } from 'next/server'
import { apiBase, apiKey } from '@/lib/server/config'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailers',
  'transfer-encoding',
  'upgrade',
  'content-encoding',
  'content-length',
])

async function forward(req: NextRequest, ctx: { params: { path: string[] } }) {
  const search = req.nextUrl.search
  const target = `${apiBase()}/api/v1/${ctx.params.path.join('/')}${search}`

  const headers = new Headers()
  headers.set('X-API-Key', apiKey())
  const contentType = req.headers.get('content-type')
  if (contentType) headers.set('Content-Type', contentType)
  const accept = req.headers.get('accept')
  if (accept) headers.set('Accept', accept)
  const lastEventId = req.headers.get('last-event-id')
  if (lastEventId) headers.set('Last-Event-ID', lastEventId)
  // Never *adds* an admin key — only relays one the operator typed this session.
  const admin = req.headers.get('x-admin-key')
  if (admin) headers.set('X-Admin-Key', admin)
  // Keeps the API's audit actor meaningful; behind a proxy every request would
  // otherwise look like the container's IP.
  const fwd = req.headers.get('x-forwarded-for')
  headers.set('X-Forwarded-For', fwd ? `${fwd}, ${req.ip ?? ''}` : (req.ip ?? ''))

  const hasBody = !['GET', 'HEAD'].includes(req.method)

  let upstream: Response
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body: hasBody ? await req.arrayBuffer() : undefined,
      signal: req.signal,
      cache: 'no-store',
      redirect: 'manual',
    })
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    return Response.json(
      { detail: `ARIA API unreachable: ${message}`, upstream: apiBase() },
      { status: 502, headers: { 'Cache-Control': 'no-store' } }
    )
  }

  const out = new Headers()
  upstream.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) out.set(key, value)
  })
  // Next compresses unless told not to, and compression buffers an SSE stream
  // into silence. FastAPI sends `no-cache`, which is not enough.
  out.set('Cache-Control', 'no-store, no-transform')
  out.set('X-Accel-Buffering', 'no')

  return new Response(upstream.body, { status: upstream.status, headers: out })
}

export const GET = forward
export const POST = forward
export const PUT = forward
export const PATCH = forward
export const DELETE = forward
export const HEAD = forward
