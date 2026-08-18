import { test, expect } from '@playwright/test'
import { ROUTES } from './routes'
import { goto, documentOverflow, overflowOffenders } from './lib'

/**
 * The defect this exists to prevent: pages laid out wider than the viewport
 * while `body { overflow-x: hidden }` clipped the evidence — the chrome stopped
 * at 390px, the content ran to 482px, and the difference read as a blank strip
 * beside the header.
 *
 * Two assertions, deliberately: `scrollWidth` alone would be satisfied by a
 * clip, so every element's right edge is checked too. Anything inside a
 * `[data-scroll-x]` container (the ScrollX primitive) is exempt — that is the
 * one sanctioned way to be wider than the column.
 */
for (const route of ROUTES) {
  test(`no horizontal overflow: ${route}`, async ({ page }) => {
    await goto(page, route)

    const offenders = await overflowOffenders(page)
    expect(offenders, `elements past the viewport on ${route}`).toEqual([])

    const overflow = await documentOverflow(page)
    expect(overflow, `document scrollWidth beyond innerWidth on ${route}`).toBeLessThanOrEqual(0)
  })
}
