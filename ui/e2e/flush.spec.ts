import { test, expect } from '@playwright/test'
import { FLUSH_ROUTES } from './routes'
import { goto } from './lib'

/**
 * Flush surfaces (chat thread, terminal) own their height: the document itself
 * must never scroll, or the composer disappears under the browser chrome and
 * the keyboard. The old chat page failed this in two ways at once — the flex
 * height chain was gated behind `lg:` so it was `display:block` on a phone, and
 * `scrollIntoView` then dragged the overflow-hidden root by ~2000px.
 */
for (const route of FLUSH_ROUTES) {
  test(`document does not scroll: ${route}`, async ({ page }) => {
    await goto(page, route)

    const { scrollHeight, innerHeight, scrollTop } = await page.evaluate(() => ({
      scrollHeight: document.documentElement.scrollHeight,
      innerHeight: window.innerHeight,
      scrollTop: document.scrollingElement?.scrollTop ?? 0,
    }))

    expect(scrollHeight, `${route} document is taller than the viewport`).toBeLessThanOrEqual(innerHeight + 1)
    expect(scrollTop, `${route} document is scrolled`).toBe(0)
  })
}
