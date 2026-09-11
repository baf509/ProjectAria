import { expect, test } from '@playwright/test'

test('temperatures retain sensor identity, expire and never wake a host', async ({ page }) => {
  const writes: string[] = []
  const stamp = new Date().toISOString()
  await page.clock.install()
  await page.route('**/api/v1/**', async route => {
    const req = route.request()
    if (req.method() !== 'GET') writes.push(req.method() + ' ' + req.url())
    const path = new URL(req.url()).pathname
    let body: unknown = {}
    if (path.endsWith('/model-servers/devices')) body = { devices: [], pools: [], temperature_hosts: [
      { node: 'red-linux', status: 'available', observed_at: stamp, max_age_seconds: 45, sensors: [
        { id: 'edge', kind: 'gpu', label: 'GPU · edge', device: '0000:03:00.0', value_c: 76, source: 'Linux hwmon' },
        { id: 'junction', kind: 'gpu', label: 'GPU · junction', device: '0000:03:00.0', value_c: 94, critical_c: 110, source: 'Linux hwmon' },
        { id: 'disk', kind: 'storage', label: 'Storage · Composite', device: 'nvme0', value_c: 51, source: 'Linux hwmon' },
        // The real shape this ranking exists for: a cool-looking DIMM that is
        // closer to its limit than the 94 °C GPU is to its own.
        { id: 'dimm', kind: 'memory', label: 'Memory · temp1', device: '2-0051', value_c: 48, high_c: 55, source: 'Linux hwmon' },
        { id: 'nic', kind: 'other', label: 'r8169 · temp1', device: 'r8169', value_c: 43, high_c: 120, source: 'Linux hwmon' },
      ] },
      { node: 'bens-macbook-pro', status: 'available', observed_at: stamp, max_age_seconds: 45, sensors: [
        { id: 'cpu', kind: 'cpu', label: 'CPU · sensor average', device: 'Apple Silicon', value_c: 48, source: 'macmon' },
        { id: 'gpu', kind: 'gpu', label: 'GPU · sensor average', device: 'Apple Silicon', value_c: null, source: 'macmon' },
      ] },
      { node: 'ridge', status: 'unavailable', observed_at: null, max_age_seconds: 45, sensors: [] },
    ] }
    else if (path.endsWith('/model-servers') || path.endsWith('/utilization')) body = { servers: [] }
    else if (path.endsWith('/services')) body = { services: [] }
    else if (path.endsWith('/llm-route')) body = { pinned: null, serving: null, loaded: [] }
    await route.fulfill({ json: body })
  })
  await page.goto('/operate')
  const red = page.getByRole('region', { name: 'red-linux temperatures' })
  await expect(red.getByText('94.0 °C')).toBeVisible()

  // Ranked by HEADROOM, not by kind or by raw temperature. The DIMM at 48 °C
  // has 7 °C of room and leads; the 94 °C GPU has 16 °C and comes second; the
  // sensors that publish no limit cannot be ranked and sort last by value.
  // Sorting these by temperature — or grouping cpu/gpu first, as this page used
  // to — buries the one sensor actually near its limit.
  // exact: '7.0 to limit' is a substring of the NIC's '77.0 to limit'.
  await expect(red.getByText('7.0 to limit', { exact: true })).toBeVisible()
  await expect(red.getByText('16.0 to limit', { exact: true })).toBeVisible()
  await expect(red.getByText('no limit given').first()).toBeVisible()
  const order = (await red.innerText()).replace(/\s+/g, ' ')
  expect(order.indexOf('48.0 °C')).toBeLessThan(order.indexOf('94.0 °C'))
  expect(order.indexOf('94.0 °C')).toBeLessThan(order.indexOf('76.0 °C'))

  // Past the four tightest, the rest collapse — but stay reachable.
  await red.getByText('1 more, with more room').click()
  await expect(red.getByText('51.0 °C')).toBeVisible()
  await expect(page.getByRole('region', { name: 'bens-macbook-pro temperatures' }).getByText('Unavailable', { exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: 'ridge temperatures' })).toContainText('No current reading.')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.clock.fastForward(50_000)
  await expect(red).toContainText('Reading expired.')
  await expect(red.getByText('94.0 °C')).toHaveCount(0)
  expect(writes).toEqual([])
})
