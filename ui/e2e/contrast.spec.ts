import { test, expect } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import { ROUTES } from './routes'
import { goto } from './lib'

/**
 * Colour contrast in both themes. The pre-rebuild tokens put every 10px
 * uppercase label at 2.7-3.2:1, and the shells page rendered
 * `text-fuchsia-200` on `bg-fuchsia-500/20` — about 1.2:1 in the light theme,
 * i.e. buttons with invisible labels.
 */
for (const route of ROUTES) {
  test(`contrast AA: ${route}`, async ({ page }) => {
    await goto(page, route)
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2aa'])
      .options({ runOnly: ['color-contrast'] })
      .analyze()
    const violations = results.violations.flatMap((v) =>
      v.nodes.map((n) => ({ target: n.target.join(' '), summary: n.failureSummary?.split('\n')[1]?.trim() }))
    )
    expect(violations, `contrast violations on ${route}`).toEqual([])
  })
}
