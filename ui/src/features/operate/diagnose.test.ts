/**
 * Tests for the fleet verdict.
 *
 * Run with `npm run test:unit` (node --test, native type stripping — the module
 * under test has type-only imports, so it needs no bundler).
 *
 * These protect the two rules that are easy to regress into a friendlier but
 * wrong answer: an unobserved backend is not a healthy one, and a sensor that
 * reports no limit can never be judged against one.
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { byHeadroom, diagnose, headroomC } from './diagnose.ts'

const machine = (over = {}) => ({
  spec: { id: 'corsair', title: 'Corsair', hint: '', node: 'corsair-ai', role: '' },
  servers: [], residents: [], meters: [],
  ...over,
}) as never

const base = { machines: [], services: [], util: [], route: undefined, servers: [] }

/* ------------------------------------------------------------- verdicts -- */

test('a fleet with nothing wrong is ok', () => {
  const d = diagnose(base as never)
  assert.equal(d.verdict, 'ok')
  assert.equal(d.headline, 'Fleet healthy')
  assert.deepEqual(d.findings, [])
})

test('an unhealthy service is critical and names the fix', () => {
  const d = diagnose({
    ...base,
    services: [{ slug: 'aria-api', state: 'stopped', healthy: false }],
  } as never)
  assert.equal(d.verdict, 'critical')
  assert.match(d.findings[0].cause, /aria-api/)
  assert.equal(d.findings[0].action, 'Start aria-api.')
})

test('an unmanageable service is reported by the page, not actioned here', () => {
  const d = diagnose({
    ...base,
    services: [{ slug: 'samba', state: 'stopped', healthy: false, manageable: false }],
  } as never)
  assert.equal(d.verdict, 'ok')
})

test('an unreachable backend is unknown, never idle', () => {
  const d = diagnose({
    ...base,
    util: [{ slug: 'red', reachable: false, probe_error: 'TimeoutError: x' }],
  } as never)
  assert.equal(d.verdict, 'unknown')
  assert.match(d.findings[0].cause, /unobserved, not idle/)
  assert.match(d.findings[0].cause, /TimeoutError/)
})

test('a probe failure with no error still degrades the verdict', () => {
  const d = diagnose({ ...base, util: [{ slug: 'red', reachable: false }] } as never)
  assert.equal(d.verdict, 'unknown')
})

test('saturation warns and says why it matters beyond latency', () => {
  const d = diagnose({
    ...base,
    util: [{ slug: 'flash', reachable: true, saturated: true }],
  } as never)
  assert.equal(d.verdict, 'warn')
  assert.match(d.findings[0].action ?? '', /cold prefill/)
})

test('declared slots drifting from observed slots warns', () => {
  const d = diagnose({
    ...base,
    util: [{ slug: 'flash', reachable: true, total_slots: 2, declared_slots: 1 }],
  } as never)
  assert.equal(d.verdict, 'warn')
  assert.match(d.findings[0].action ?? '', /without a restart/)
})

test('a spilling pool is critical', () => {
  const d = diagnose({
    ...base,
    machines: [machine({ meters: [{ key: 'vram', title: 'R9700 VRAM', free_gib: 0, warn: 'Spilling…' }] })],
  } as never)
  assert.equal(d.verdict, 'critical')
  assert.match(d.findings[0].cause, /spilling/i)
})

test('a pin that disagrees with what is answering warns', () => {
  const d = diagnose({ ...base, route: { pinned: 'a', serving: 'b' } } as never)
  assert.equal(d.verdict, 'warn')
  assert.match(d.findings[0].cause, /pinned to a/)
})

test('the worst finding sets the verdict and sorts first', () => {
  const d = diagnose({
    ...base,
    services: [{ slug: 'aria-api', healthy: false }],
    util: [{ slug: 'flash', reachable: true, saturated: true }],
  } as never)
  assert.equal(d.verdict, 'critical')
  assert.equal(d.findings[0].verdict, 'critical')
  assert.equal(d.findings.length, 2)
})

/* -------------------------------------------------------------- thermal -- */

const temps = (sensors: unknown[]) => machine({
  temps: { node: 'corsair-ai', status: 'available', observed_at: null, max_age_seconds: 45, sensors },
})

test('a sensor past its critical limit is critical', () => {
  const d = diagnose({
    ...base,
    machines: [temps([{ id: '1', kind: 'gpu', label: 'GPU', device: 'x', value_c: 95, critical_c: 90, source: 's' }])],
  } as never)
  assert.equal(d.verdict, 'critical')
})

test('a hot sensor that reports NO limit cannot raise a finding', () => {
  // Corsair's CPU sits at 72 °C and reports no limit at all. Inventing one
  // would make the page cry wolf on every box that does not publish limits.
  const d = diagnose({
    ...base,
    machines: [temps([{ id: '1', kind: 'cpu', label: 'Tctl', device: 'x', value_c: 72, source: 's' }])],
  } as never)
  assert.equal(d.verdict, 'ok')
})

test('expired temperature telemetry is not read as cool', () => {
  const d = diagnose({
    ...base,
    machines: [machine({
      temps: {
        node: 'x', status: 'expired', observed_at: null, max_age_seconds: 45,
        sensors: [{ id: '1', kind: 'gpu', label: 'GPU', device: 'x', value_c: 99, critical_c: 90, source: 's' }],
      },
    })],
  } as never)
  assert.equal(d.verdict, 'ok')   // unobservable, so it contributes nothing
})

/* ------------------------------------------------------------- headroom -- */

test('headroom is null when no limit is reported', () => {
  assert.equal(headroomC({ value_c: 72 }), null)
  assert.equal(headroomC({ value_c: null, high_c: 55 }), null)
  // Float arithmetic: the UI rounds for display, so compare the same way.
  assert.equal((headroomC({ value_c: 37.8, high_c: 55 }) as number).toFixed(1), '17.2')
})

test('critical_c is used when high_c is absent', () => {
  assert.equal(headroomC({ value_c: 39, critical_c: 110 }), 71)
})

test('sensors sort by headroom, limitless ones last but still present', () => {
  // Red's real shape: the DIMMs are 17 °C from their limit while every GPU has
  // 70+ °C of room, and the hottest number on the box reports no limit at all.
  const sorted = byHeadroom([
    { value_c: 39, critical_c: 110 },          // 71 to go
    { value_c: 72 },                           // hot, unrankable
    { value_c: 37.8, high_c: 55 },             // 17.2 to go — tightest
  ])
  assert.deepEqual(sorted.map((s) => s.value_c), [37.8, 39, 72])
})

test('the tightest real margin is named in context, unrankable sensors excluded', () => {
  const d = diagnose({
    ...base,
    machines: [temps([
      { id: '1', kind: 'cpu', label: 'Tctl', device: 'x', value_c: 72, source: 's' },
      { id: '2', kind: 'memory', label: 'DIMM 2-0051', device: 'y', value_c: 37.8, high_c: 55, source: 's' },
    ])],
  } as never)
  assert.ok(d.context.some((c) => c.includes('17.2 °C') && c.includes('DIMM 2-0051')))
})

test('context answers whether another model fits', () => {
  const d = diagnose({
    ...base,
    machines: [machine({ meters: [{ key: 'vram', title: 'RTX 3090 VRAM', free_gib: 1.3 }] })],
  } as never)
  assert.ok(d.context.some((c) => c.includes('1.3 GiB free')))
})
