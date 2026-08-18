import { test, expect } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'

/**
 * Deploy-integrity checks.
 *
 * These exist because of a real miss: `npx next build` does NOT run npm's
 * `prebuild`/`postbuild` scripts, so the token CSS, the build stamp and the
 * service worker were silently left at their previous versions while the app
 * code moved on. The served worker was months old, and nothing in the app said
 * so — the failure mode of a stale worker is a page that behaves like an older
 * build for reasons nobody can see.
 */

const buildId = () =>
  fs.readFileSync(path.join(process.cwd(), '.next/BUILD_ID'), 'utf8').trim()

test('the served service worker was generated for THIS build', async ({ request }) => {
  const res = await request.get('/sw.js')
  expect(res.status(), 'sw.js must be served').toBe(200)
  const body = await res.text()
  // gen-sw.mjs emits the id via JSON.stringify, i.e. double quotes.
  const m = body.match(/const VERSION = ["']([^"']+)["']/)
  expect(m, 'sw.js must be the generated one (scripts/gen-sw.mjs), not a hand-written leftover').toBeTruthy()
  expect(m![1], 'sw.js VERSION must match .next/BUILD_ID — run `npm run build`, not `npx next build`').toBe(buildId())
})

test('the service worker never intercepts API or stream traffic', async ({ request }) => {
  const body = await (await request.get('/sw.js')).text()
  // Data must never be served from a cache to an operator, and an SSE stream
  // cannot survive being cached at all.
  expect(body).toContain("/api/")
  expect(body).toMatch(/text\/event-stream/)
})

test('the build stamp is present and matches the served build', async ({ request }) => {
  const res = await request.get('/api/build')
  expect(res.status()).toBe(200)
  const info = await res.json()
  expect(info.sha, 'build stamp must carry a sha').toBeTruthy()
  expect(info.date, 'build stamp must carry a date').toBeTruthy()
})

test('a first visit does not reload itself (no duplicated requests)', async ({ page }) => {
  const calls: string[] = []
  page.on('request', (r) => {
    if (r.url().includes('/api/v1/')) calls.push(r.url())
  })
  await page.goto('/inbox', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3500)

  const counts = new Map<string, number>()
  for (const c of calls) counts.set(c, (counts.get(c) ?? 0) + 1)
  const duplicated = [...counts.entries()].filter(([, n]) => n > 1).map(([u]) => u)

  // The service worker claiming its client fires `controllerchange`; reloading
  // on that fires every request on the page a second time.
  expect(duplicated, 'requests issued twice — is sw-register reloading on first install?').toEqual([])
})
