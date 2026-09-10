import { expect, test, type Page } from '@playwright/test'

const FLASH = 'Qwen3.8-Flash-Next-CUDA-Halo-Candidate'
const writes: string[] = []

async function fixture(page: Page, startable: boolean, running = false, failStart = false) {
  writes.length = 0
  const server = { slug: FLASH, state: running ? 'running' : 'exited', startable,
    onbox: true, memory_pool: 'halo-gtt', also_uses: ['nvidia-vram'],
    not_startable_reason: startable ? null : 'Qualification pending', resident_gib_estimate: 100 }
  let pinned: string | null = null
  await page.route('**/api/v1/**', async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname.replace('/api/v1', '')
    if (req.method() !== 'GET') writes.push(`${req.method()} ${path}`)
    let body: unknown = {}
    if (path === '/infrastructure/model-servers') body = { servers: [server] }
    else if (path === '/infrastructure/model-servers/devices') body = { devices: [], pools: [] }
    else if (path === '/infrastructure/services') body = { services: [] }
    else if (path === '/infrastructure/model-servers/utilization') body = { servers: [] }
    else if (path === `/infrastructure/model-servers/${FLASH}/start`) {
      expect(req.postData()).toBeFalsy() // Never force an eviction or override the profile.
      if (failStart) {
        await route.fulfill({ status: 409, json: { detail: 'Conflicting residency; review required' } })
        return
      }
      server.state = 'running'
      body = server
    } else if (path === `/infrastructure/model-servers/${FLASH}`) body = server
    else if (path === '/infrastructure/llm-route') {
      if (req.method() === 'PUT') {
        expect(req.postDataJSON()).toEqual({ slug: FLASH })
        pinned = FLASH
      }
      body = { pinned, serving: server.state === 'running' ? FLASH : null, loaded: [] }
    }
    await route.fulfill({ json: body })
  })
  await page.goto('/operate', { waitUntil: 'domcontentloaded' })
  return page.getByRole('button', { name: 'Load and select Flash Next', exact: true })
}

test('qualification gate disables the new loadout and retired dual button is absent', async ({ page }) => {
  const button = await fixture(page, false)
  await expect(button).toBeDisabled()
  await expect(page.getByText('Qualification pending', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Load Qwen dual resident' })).toHaveCount(0)
  expect(writes).toEqual([])
})

test('qualified loadout starts only the selected model and pins it after readiness', async ({ page }) => {
  const button = await fixture(page, true)
  await button.click()
  await expect(button).toHaveAttribute('aria-pressed', 'true')
  expect(writes).toEqual([`POST /infrastructure/model-servers/${FLASH}/start`, 'PUT /infrastructure/llm-route'])
})

test('already resident selection does not restart or stop any model', async ({ page }) => {
  const button = await fixture(page, true, true)
  await button.click()
  await expect(button).toHaveAttribute('aria-pressed', 'true')
  expect(writes).toEqual(['PUT /infrastructure/llm-route'])
})

test('a rejected start does not change the route or stop another deployment', async ({ page }) => {
  const button = await fixture(page, true, false, true)
  await button.click()
  await expect(page.getByText('loadout: Conflicting residency; review required')).toBeVisible()
  expect(writes).toEqual([`POST /infrastructure/model-servers/${FLASH}/start`])
})
