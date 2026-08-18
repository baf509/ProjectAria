import { test, expect } from '@playwright/test'
import { ROUTES, TOUCH_PROJECTS } from './routes'
import { goto, smallTargets, tinyText, smallFormControls } from './lib'

/**
 * The phone floors. Measured before the rebuild: 67 of 67 interactive elements
 * on /inbox were under 44px, 247 of 316 text nodes were under 12px, and every
 * form control was under 16px — which is what makes iOS zoom into a field on
 * focus and never zoom back out.
 */
test.describe('touch floors', () => {
  for (const route of ROUTES) {
    test(`44px targets, 12px type, 16px controls: ${route}`, async ({ page }, testInfo) => {
      test.skip(!TOUCH_PROJECTS.includes(testInfo.project.name), 'touch projects only')
      await goto(page, route)

      expect(await smallTargets(page), `sub-44px tap targets on ${route}`).toEqual([])
      expect(await tinyText(page, 12), `text under 12px on ${route}`).toEqual([])
      expect(await smallFormControls(page), `form controls under 16px on ${route}`).toEqual([])
    })
  }
})
