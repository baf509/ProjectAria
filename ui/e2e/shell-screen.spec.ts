import { test, expect } from '@playwright/test'

// Browser contract test. Fake only the disposable shell's data and stream;
// exercising the real terminal component and visibility/reconnect lifecycle.
test('screen streams reconnect, stop when hidden, and leave input to the correct shell', async ({ page }) => {
  const name = 'claude-screen-contract'
  await page.route(`**/api/v1/shells/${name}`, route => route.fulfill({ json: {
    name, short_name: 'screen-contract', status: 'active', host: 'mac',
    line_count: 3, project_dir: '/tmp', tags: [], last_activity_at: new Date().toISOString(),
  } }))
  let input: Record<string, unknown> | undefined
  await page.route(`**/api/v1/shells/${name}/input`, route => {
    input = route.request().postDataJSON()
    return route.fulfill({ json: { ok: true, line_number: 4, screen: null } })
  })
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    const state = { opened: 0, active: 0, screen: 'INITIAL_SCREEN',
      push: (_text: string) => {}, fail: () => {} }
    ;(window as any).__screenTest = state
    window.fetch = (url, init) => {
      if (!String(url).endsWith('/claude-screen-contract/screen/stream')) return original(url, init)
      state.opened++
      state.active++
      let ended = false
      let controller: ReadableStreamDefaultController<Uint8Array>
      const finish = () => { if (!ended) { ended = true; state.active-- } }
      const stream = new ReadableStream<Uint8Array>({
        start(c) {
          controller = c
          state.push = text => {
            state.screen = text
            if (!ended) c.enqueue(new TextEncoder().encode(`event: screen\ndata: ${JSON.stringify({ name: 'claude-screen-contract', lines: 1, screen: text })}\n\n`))
          }
          state.push(state.screen)
          state.fail = () => { finish(); c.error(new Error('test disconnect')) }
        },
        cancel() { finish() },
      })
      init?.signal?.addEventListener('abort', () => { finish(); controller.error(new DOMException('Aborted', 'AbortError')) }, { once: true })
      return Promise.resolve(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }))
    }
  })
  await page.goto(`/supervise/shells/${name}`)
  await expect(page.getByText('INITIAL_SCREEN', { exact: true })).toBeVisible()
  await page.evaluate(() => (window as any).__screenTest.push('LATEST_SCREEN'))
  await expect(page.getByText('LATEST_SCREEN', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Enter', exact: true }).click()
  expect(input?.wait_ms).toBe(0)
  expect(input?.text).toBe('Enter')
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect.poll(() => page.evaluate(() => (window as any).__screenTest.active)).toBe(0)
  const count = await page.evaluate(() => (window as any).__screenTest.opened)
  await page.waitForTimeout(1000)
  expect(await page.evaluate(() => (window as any).__screenTest.opened)).toBe(count)
  await page.evaluate(() => {
    ;(window as any).__screenTest.screen = 'AFTER_RESUME'
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(page.getByText('AFTER_RESUME', { exact: true })).toBeVisible()
  await page.evaluate(() => (window as any).__screenTest.fail())
  await expect.poll(() => page.evaluate(() => (window as any).__screenTest.opened)).toBe(count + 2)
  await expect.poll(() => page.evaluate(() => (window as any).__screenTest.active)).toBe(1)
  await page.getByRole('button', { name: 'Follow', exact: true }).click()
  await expect.poll(() => page.evaluate(() => (window as any).__screenTest.active)).toBe(0)
})
