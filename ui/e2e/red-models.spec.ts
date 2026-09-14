import { expect, test, type Page } from '@playwright/test'
import { documentOverflow } from './lib'
const C = 'Qwen3.8-Flash-Next-CUDA-Halo-Candidate'
const R = 'Red-Qwen3.8-27B-MXFP4'
const P = 'Red-Qwen3.8-27B-PARO-INT5'
const F = 'Red-Qwen3.8-Flash-Next-MXFP4'
async function setup(page: Page, opts: { paroLoaded?: boolean; pinned?: string; busy?: boolean; stopFails?: boolean; startFails?: boolean; missing?: boolean; stopped?: boolean; noAdmin?: boolean; unknownActivity?: boolean } = {}) {
  const writes: string[] = []
  let pinned: string | null = opts.pinned ?? C
  const servers = [
    { slug: P, state: opts.paroLoaded ? 'running' : 'stopped', startable: true, onbox: false, remotely_operable: true, host_machine: 'machine:red', catalog_visible: true },
    { slug: C, state: 'running', startable: true, onbox: true, host_machine: 'machine:corsair', catalog_visible: true },
    { slug: R, state: opts.stopped || opts.paroLoaded ? 'stopped' : 'running', startable: true, onbox: false, remotely_operable: true, host_machine: 'machine:red', catalog_visible: true },
    ...opts.missing ? [] : [{ slug: F, state: 'stopped', startable: true, onbox: false, remotely_operable: true, host_machine: 'machine:red', catalog_visible: true }],
    { slug: 'context1-Q4', state: 'stopped', startable: false, onbox: true, host_machine: 'machine:corsair', catalog_visible: false },
    { slug: 'gemma-4-e4b-Q4', state: 'stopped', startable: false, onbox: true, host_machine: 'machine:mac', catalog_visible: false },
  ]
  await page.route('**/api/v1/**', async route => {
    const req = route.request(); const path = new URL(req.url()).pathname.replace('/api/v1', '')
    if (req.method() !== 'GET') writes.push(`${req.method()} ${path}`)
    let body: unknown = {}
    if (path === '/infrastructure/model-servers') body = { servers }
    else if (path === '/infrastructure/model-servers/utilization') body = { servers: [{ slug: opts.paroLoaded ? P : R, reachable: true, busy_slots: opts.unknownActivity ? null : opts.busy ? 1 : 0 }] }
    else if (path === '/infrastructure/model-servers/devices') body = { devices: [], pools: [] }
    else if (path === '/infrastructure/services') body = { services: [] }
    else if (path === '/infrastructure/llm-route') {
      if (req.method() === 'PUT') pinned = req.postDataJSON().slug
      body = { pinned, serving: pinned, loaded: servers.filter(s => s.state === 'running').map(s => ({ slug: s.slug })) }
    } else if (path.startsWith('/infrastructure/model-servers/')) {
      const [, slug, action] = path.match(/model-servers\/([^/]+)(?:\/(start|stop))?$/) ?? []
      const server = servers.find(s => s.slug === slug)
      if (action) {
        expect(slug).not.toBe(C)
        expect(req.postData()).toBeFalsy()
        if ((action === 'stop' && opts.stopFails) || (action === 'start' && opts.startFails)) {
          await route.fulfill({ status: 409, json: { detail: `${action} refused by Red` } }); return
        }
        if (server) server.state = action === 'start' ? 'running' : 'stopped'
      }
      body = server
    }
    await route.fulfill({ json: body })
  })
  await page.goto('/operate', { waitUntil: 'domcontentloaded' })
  await expect(page.getByText(opts.missing ? '3 available' : '4 available', { exact: true })).toBeVisible({ timeout: 45000 })
  if ((opts.pinned === R || opts.pinned === P) && !opts.noAdmin) {
    const mobile = (page.viewportSize()?.width ?? 1280) < 1024
    if (mobile) await page.getByRole('button', { name: 'More', exact: true }).click()
    await page.locator('input[placeholder="X-Admin-Key"]:visible').fill('test-admin');
    await page.getByRole('button', { name: 'Use for this session', exact: true }).click()
    if (mobile) await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click()
  }
  return { writes, servers, pinned: () => pinned }
}

test('switch unloads Red first, loads the selected model, and keeps Corsair selected', async ({ page }) => {
  const x = await setup(page)
  await page.getByRole('button', { name: 'Switch to Qwen Flash Next', exact: true }).click()
  await expect(page.getByText('Qwen Flash Next is loaded on Red.', { exact: true })).toBeVisible()
  expect(x.writes).toEqual([`POST /infrastructure/model-servers/${R}/stop`, `POST /infrastructure/model-servers/${F}/start`])
  expect(x.pinned()).toBe(C)
  expect(await documentOverflow(page)).toBeLessThanOrEqual(1)
})

test('switch carries an existing Red route pin to the replacement only after it loads', async ({ page }) => {
  const x = await setup(page, { pinned: R })
  await page.getByRole('button', { name: 'Switch to Qwen Flash Next', exact: true }).click()
  await expect(page.getByText('Qwen Flash Next is loaded on Red.', { exact: true })).toBeVisible()
  expect(x.writes).toEqual([`POST /infrastructure/model-servers/${R}/stop`, `POST /infrastructure/model-servers/${F}/start`, 'PUT /infrastructure/llm-route'])
  expect(x.pinned()).toBe(F)
})

test('unload failure never starts the replacement', async ({ page }) => {
  const x = await setup(page, { stopFails: true })
  await page.getByRole('button', { name: 'Switch to Qwen Flash Next', exact: true }).click()
  await expect(page.getByRole('main').getByRole('alert')).toContainText('stop refused by Red')
  expect(x.writes).toEqual([`POST /infrastructure/model-servers/${R}/stop`])
})

test('load failure remains visible and never announces readiness or changes route', async ({ page }) => {
  const x = await setup(page, { startFails: true, pinned: R })
  await page.getByRole('button', { name: 'Switch to Qwen Flash Next', exact: true }).click()
  await expect(page.getByRole('main').getByRole('alert')).toContainText('start refused by Red')
  await expect(page.getByText('Qwen Flash Next is loaded on Red.', { exact: true })).toHaveCount(0)
  expect(x.pinned()).toBe(R)
  expect(x.writes).toHaveLength(2)
})

test('active requests prevent model switching and unloading', async ({ page }) => {
  const x = await setup(page, { busy: true })
  await expect(page.getByRole('button', { name: 'Switch to Qwen Flash Next', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Unload Qwen3.8-27B', exact: true })).toBeDisabled()
  expect(x.writes).toEqual([])
})

test('cold load starts only the selected Red model', async ({ page }) => {
  const x = await setup(page, { stopped: true })
  await page.getByRole('button', { name: 'Load Qwen Flash Next', exact: true }).click()
  await expect(page.getByText('Qwen Flash Next is loaded on Red.', { exact: true })).toBeVisible()
  expect(x.writes).toEqual([`POST /infrastructure/model-servers/${F}/start`])
})

test('unload leaves Red empty and removes only its own route pin', async ({ page }) => {
  const x = await setup(page, { pinned: R })
  await page.getByRole('button', { name: 'Unload Qwen3.8-27B', exact: true }).click()
  await expect(page.getByText('Red is unloaded. All models remain available to load.', { exact: true })).toBeVisible()
  expect(x.writes).toEqual([`POST /infrastructure/model-servers/${R}/stop`, 'PUT /infrastructure/llm-route'])
  expect(x.pinned()).toBe(null)
})

test('Gemma and retired Corsair entries are absent; an unavailable new Red model is disabled', async ({ page }) => {
  await setup(page, { missing: true })
  await expect(page.getByRole('link', { name: /gemma|context1/i })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Switch to Qwen Flash Next', exact: true })).toBeDisabled()
  await expect(page.getByText('Available after the pending Aria update is activated.', { exact: true })).toBeVisible()
})


test('a pinned Red model requires route credentials before any unload', async ({ page }) => {
  const x = await setup(page, { pinned: R, noAdmin: true })
  await page.getByRole('button', { name: 'Switch to Qwen Flash Next', exact: true }).click()
  await expect(page.getByRole('main').getByRole('alert')).toContainText('Add your admin key')
  expect(x.writes).toEqual([])
})

test('unknown request activity is not treated as idle', async ({ page }) => {
  const x = await setup(page, { unknownActivity: true })
  await expect(page.getByRole('button', { name: 'Switch to Qwen Flash Next', exact: true })).toBeDisabled()
  await expect(page.getByText('Checking Red activity before enabling model changes…')).toBeVisible()
  expect(x.writes).toEqual([])
})


test('PARO promotion unloads the existing 27B before loading int5', async ({ page }) => {
  const x = await setup(page)
  await page.getByRole('button', { name: 'Switch to Qwen3.8-27B PARO int5', exact: true }).click()
  await expect(page.getByText('Qwen3.8-27B PARO int5 is loaded on Red.', { exact: true })).toBeVisible()
  expect(x.writes).toEqual([`POST /infrastructure/model-servers/${R}/stop`, `POST /infrastructure/model-servers/${P}/start`])
  expect(x.pinned()).toBe(C)
  expect(await documentOverflow(page)).toBeLessThanOrEqual(1)
})

test('switching away from resident PARO unloads it and carries its explicit pin', async ({ page }) => {
  const x = await setup(page, { paroLoaded: true, pinned: P })
  await page.getByRole('button', { name: 'Switch to Qwen3.8-27B', exact: true }).click()
  await expect(page.getByText('Qwen3.8-27B is loaded on Red.', { exact: true })).toBeVisible()
  expect(x.writes).toEqual([`POST /infrastructure/model-servers/${P}/stop`, `POST /infrastructure/model-servers/${R}/start`, 'PUT /infrastructure/llm-route'])
  expect(x.pinned()).toBe(R)
})

test('PARO in-flight requests prevent ordinary switching', async ({ page }) => {
  const x = await setup(page, { paroLoaded: true, busy: true })
  await expect(page.getByRole('button', { name: 'Switch to Qwen3.8-27B', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Unload Qwen3.8-27B PARO int5', exact: true })).toBeDisabled()
  expect(x.writes).toEqual([])
})
