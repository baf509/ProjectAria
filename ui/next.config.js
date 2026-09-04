const buildSha = process.env.BUILD_SHA

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Keep production artifacts and the generated service-worker cache version
  // reproducible across source and deployed clones. `next build` otherwise
  // creates a fresh random ID even when every input is identical.
  ...(buildSha ? { generateBuildId: async () => buildSha } : {}),
  // Standalone only for the container image (the Dockerfile sets this). Locally
  // it would break `next start`, which the responsive gate uses to serve the
  // production build — and a gate that cannot serve real CSS silently passes an
  // unstyled document.
  ...(process.env.NEXT_OUTPUT === 'standalone' ? { output: 'standalone' } : {}),
  poweredByHeader: false,
  // NOTE: no `env` block. The API URL and key are read at REQUEST time by the
  // proxy route handler (src/lib/server/config.ts) from ARIA_API_URL /
  // ARIA_API_KEY, so changing either is `docker compose up -d ui`, not a
  // rebuild — and neither reaches the browser bundle.
  async redirects() {
    return [
      // Routes renamed 2026-08-17 so the URL matches the area it belongs to.
      { source: '/', destination: '/inbox', permanent: true },
      { source: '/chat', destination: '/converse', permanent: true },
      { source: '/chat/:id', destination: '/converse/:id', permanent: true },
      { source: '/cockpit', destination: '/supervise', permanent: true },
      { source: '/cockpit/:slug', destination: '/supervise/projects/:slug', permanent: true },
      { source: '/dashboard/shells', destination: '/supervise/shells', permanent: true },
      { source: '/dashboard/benchmarks', destination: '/operate/benchmarks', permanent: true },
      // Tab-as-state deep links must survive the split into route segments.
      // `/dashboard?tab=conversations` moved area entirely (Know -> Converse).
      {
        source: '/dashboard',
        has: [{ type: 'query', key: 'tab', value: 'conversations' }],
        destination: '/converse',
        permanent: true,
      },
      {
        source: '/dashboard',
        has: [{ type: 'query', key: 'tab', value: '(?<tab>memories|tasks|research|workflows|usage|agents)' }],
        destination: '/know/:tab',
        permanent: true,
      },
      { source: '/dashboard', destination: '/know/memories', permanent: true },
      { source: '/know', destination: '/know/memories', permanent: true },
    ]
  },
}

module.exports = nextConfig
