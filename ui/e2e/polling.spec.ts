import { test, expect } from '@playwright/test'
import { goto, settle } from './lib'

/**
 * Polling hygiene. Before the data layer existed, nine components each owned a
 * `setInterval` that kept firing while the PWA was backgrounded, and the same
 * 73KB fleet payload was fetched independently by three routes.
 */
test('a hidden tab issues no requests', async ({ page }) => {
  await goto(page, '/inbox')

  const seen: string[] = []
  page.on('request', (r) => {
    if (r.url().includes('/api/v1/')) seen.push(r.url())
  })

  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })

  await page.waitForTimeout(20_000)
  expect(seen, 'requests fired while the tab was hidden').toEqual([])
})

test('client-side navigation back to a route reuses the cache', async ({ page }) => {
  // NOTE: this must be a CLIENT-SIDE navigation (tapping the nav), not
  // page.goto(). A full reload wipes the in-memory SWR cache by design —
  // persistence to storage is deliberately deferred until destructive actions
  // are gated on freshness, because a persisted "running" on an operator
  // surface is worse than a refetch. The first version of this test used
  // page.goto() and was asserting something the architecture does not promise.
  await goto(page, '/operate')

  const fleetCalls: string[] = []
  page.on('request', (r) => {
    const u = r.url()
    if (u.includes('/infrastructure/model-servers') && !u.includes('utilization') && !u.includes('devices')) {
      fleetCalls.push(u)
    }
  })

  // The visible nav depends on the viewport: bottom tabs below lg, rail above.
  const tab = (href: string) =>
    page.locator(`nav a[href="${href}"]`).filter({ hasNotText: /^ARIA$/ }).locator('visible=true').first()
  await tab('/inbox').click()
  await page.waitForURL('**/inbox')
  await settle(page, 1000)
  await tab('/operate').click()
  await page.waitForURL('**/operate')
  await settle(page, 1500)

  // The 73KB fleet payload is on the 30s tier, so a round trip inside that
  // window must be served from cache.
  expect(fleetCalls.length, 'fleet payload refetched on back-navigation').toBe(0)
})
