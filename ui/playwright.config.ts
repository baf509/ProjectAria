import { defineConfig, devices } from '@playwright/test'

/**
 * The responsive gate.
 *
 * Device capability matters more than width here: the touch projects set
 * `hasTouch`, which is what makes `(pointer: coarse)` match and therefore what
 * the 44px/12px/16px floors are actually tested against.
 *
 * WebKit cannot launch on this box (missing system libs, needs root), so iOS
 * Safari behaviour is covered by construction (dvh, --vvh, 16px controls,
 * safe-area tokens) plus a manual phone pass per phase. The MacBook is an
 * aria-node and can run this same config with the webkit project against
 * corsair:3100 over the tailnet.
 */
export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: process.env.GATE_URL || 'http://localhost:3100',
    trace: 'retain-on-failure',
  },
  webServer: process.env.GATE_NO_SERVER
    ? undefined
    : {
        command: './e2e/serve.sh',
        url: 'http://localhost:3100/inbox',
        reuseExistingServer: true,
        timeout: 120_000,
      },
  projects: [
    {
      // Defined explicitly rather than spreading devices['iPhone SE']: that
      // descriptor carries `defaultBrowserType: 'webkit'`, which silently
      // switched this project to a browser that cannot launch on this host —
      // 35 "failures" that were all the same missing-system-library error
      // wearing the costume of contrast and overflow defects.
      name: 'phone-se',
      use: {
        browserName: 'chromium',
        viewport: { width: 375, height: 667 },
        hasTouch: true,
        isMobile: true,
        deviceScaleFactor: 2,
      },
    },
    {
      name: 'phone',
      use: { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 3 },
    },
    {
      name: 'phone-landscape',
      use: { viewport: { width: 844, height: 390 }, hasTouch: true, isMobile: true },
    },
    {
      name: 'slideover',
      // iPad Slide Over is 320px — below the width everyone designs to.
      use: { viewport: { width: 320, height: 568 }, hasTouch: true, isMobile: true },
    },
    {
      name: 'tablet',
      use: { viewport: { width: 768, height: 1024 }, hasTouch: true, isMobile: true },
    },
    {
      name: 'laptop',
      use: { viewport: { width: 1280, height: 800 } },
    },
    {
      name: 'phone-dark',
      use: { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, colorScheme: 'dark' },
    },
  ],
})
