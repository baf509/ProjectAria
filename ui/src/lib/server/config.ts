/**
 * ARIA - server-side runtime configuration
 *
 * Read at REQUEST time, never baked. Before this, `NEXT_PUBLIC_API_URL` and
 * `NEXT_PUBLIC_API_KEY` were compiled into the client bundle at image build
 * time, with three different stale `:8000` fallbacks in the source tree — so
 * changing the API URL or rotating the key meant rebuilding the image.
 */

const DEFAULT_BASE = 'http://127.0.0.1:8200'

export function apiBase(): string {
  return (process.env.ARIA_API_URL || DEFAULT_BASE).replace(/\/$/, '')
}

export function apiKey(): string {
  const key = process.env.ARIA_API_KEY
  if (!key) {
    // Fail loudly rather than sending unauthenticated requests that 401 with no
    // CORS headers and surface as an opaque network error.
    throw new Error('ARIA_API_KEY is not set — the UI container cannot reach the API')
  }
  return key
}
