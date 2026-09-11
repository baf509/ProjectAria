/**
 * ARIA - Operate: the fleet verdict
 *
 * /operate states what is true. It has never stated whether that is GOOD, so
 * deciding "is the fleet healthy?" means reading six machine cards, the service
 * list and the route box, and joining them by hand. This module does that join
 * once and returns a verdict, a cause, and — when there is something to do — an
 * action.
 *
 * Deliberately a pure function over the payloads the page already fetches: no
 * new endpoint, no history, no request of its own. It is also deliberately
 * conservative. Two rules it must never break:
 *
 * 1. **Unknown is not healthy.** A probe that failed and a model that is idle
 *    are different facts (`probe_error` exists precisely because they were once
 *    indistinguishable), so an unreachable backend degrades the verdict to
 *    `unknown` rather than passing silently.
 * 2. **A missing limit is not a passing limit.** Most sensors here report no
 *    `high_c`/`critical_c` at all. A sensor with no limit can be reported, but
 *    can never raise or clear a thermal finding.
 */
import type {
  LlmRouteFull,
  ModelServerFull,
  ServiceFull,
  TemperatureHost,
  UtilServer,
} from '@/lib/api/types'
import type { Machine } from './machines'

/** Ordered worst-first: the page leads with the worst news. */
export type Verdict = 'critical' | 'warn' | 'unknown' | 'ok'

export type Finding = {
  verdict: Verdict
  /** What is true, in one clause. */
  cause: string
  /** What to do about it. Absent when there is nothing to do. */
  action?: string
}

export type Diagnosis = {
  verdict: Verdict
  headline: string
  /** Everything that contributed, worst first. */
  findings: Finding[]
  /** Facts worth stating that are not problems — the "why OK is OK" line. */
  context: string[]
}

const RANK: Record<Verdict, number> = { critical: 0, warn: 1, unknown: 2, ok: 3 }
const worst = (a: Verdict, b: Verdict): Verdict => (RANK[a] <= RANK[b] ? a : b)

const HEADLINE: Record<Verdict, string> = {
  critical: 'Attention needed',
  warn: 'Degraded',
  unknown: 'Partly unobserved',
  ok: 'Fleet healthy',
}

/** A sensor can only be judged against a limit it actually reports. */
export function headroomC(sensor: { value_c: number | null; high_c?: number | null; critical_c?: number | null }) {
  const limit = sensor.high_c ?? sensor.critical_c ?? null
  if (limit === null || sensor.value_c === null || !Number.isFinite(sensor.value_c)) return null
  return limit - sensor.value_c
}

/**
 * Sensors ordered by how close they are to their own limit.
 *
 * Red reports 22 sensors; rendered flat, a GPU at 33 °C and a DIMM 17 °C from
 * its limit look identical. Sensors with no limit sort last and keep their
 * reading — they are still worth showing, they just cannot be ranked.
 */
export function byHeadroom<T extends { value_c: number | null; high_c?: number | null; critical_c?: number | null }>(
  sensors: T[]
): T[] {
  return [...sensors].sort((a, b) => {
    const ha = headroomC(a)
    const hb = headroomC(b)
    if (ha === null && hb === null) return (b.value_c ?? -Infinity) - (a.value_c ?? -Infinity)
    if (ha === null) return 1
    if (hb === null) return -1
    return ha - hb
  })
}

/** Thermal findings for one host, if any of its sensors report a limit. */
function thermal(host: TemperatureHost | undefined, title: string): Finding[] {
  if (!host || host.status !== 'available') return []
  const out: Finding[] = []
  for (const s of host.sensors) {
    if (s.value_c === null || !Number.isFinite(s.value_c)) continue
    if (s.critical_c != null && s.value_c >= s.critical_c) {
      out.push({
        verdict: 'critical',
        cause: `${title} ${s.label} at ${s.value_c.toFixed(1)} °C — at or past its ${s.critical_c} °C critical limit`,
        action: 'Reduce load on this host or stop its resident model.',
      })
    } else if (s.high_c != null && s.value_c >= s.high_c) {
      out.push({
        verdict: 'warn',
        cause: `${title} ${s.label} at ${s.value_c.toFixed(1)} °C — past its ${s.high_c} °C high limit`,
      })
    }
  }
  return out
}

export function diagnose({
  machines,
  services,
  util,
  route,
  servers,
}: {
  machines: Machine[]
  services: ServiceFull[] | undefined
  util: UtilServer[] | undefined
  route: LlmRouteFull | undefined
  servers: ModelServerFull[] | undefined
}): Diagnosis {
  const findings: Finding[] = []
  const context: string[] = []

  /* -- services: an always-up service that is down is the worst news here -- */
  // `healthy` is the server's own verdict and already folds in `expected_state`
  // — an on-demand service that is stopped is healthy, an always-up one is not.
  // Reimplementing that rule here would give the page two definitions of down.
  const down = (services ?? []).filter((s) => !s.healthy && s.manageable !== false)
  for (const s of down) {
    findings.push({
      verdict: 'critical',
      cause: `${s.slug} is ${s.state ?? 'down'} — expected always up`,
      action: `Start ${s.slug}.`,
    })
  }

  /* ------------------------------------------------- model plane: serving -- */
  const reachable = (util ?? []).filter((u) => u.reachable)
  const unreachable = (util ?? []).filter((u) => !u.reachable)

  for (const u of unreachable) {
    findings.push({
      verdict: 'unknown',
      // The distinction probe_error was added for: a probe that RAISED and a
      // server that is DOWN both land here and must not read the same.
      cause: u.probe_error
        ? `${u.slug} telemetry failed (${u.probe_error}) — its load is unobserved, not idle`
        : `${u.slug} did not answer its telemetry probe — its load is unobserved, not idle`,
      action: 'Check the backend before trusting any throughput number for it.',
    })
  }

  for (const u of reachable) {
    if (u.saturated) {
      findings.push({
        verdict: 'warn',
        cause: `${u.slug} is saturated — every slot busy and requests queuing`,
        // Why this matters beyond latency, which is the non-obvious part.
        action: 'A queued request lands in whichever slot frees first, so warm prefixes degrade into a cold prefill per turn.',
      })
    }
    if (
      u.declared_slots != null && u.total_slots != null &&
      u.declared_slots !== u.total_slots
    ) {
      findings.push({
        verdict: 'warn',
        cause: `${u.slug} is serving ${u.total_slots} slots but its launch file declares ${u.declared_slots}`,
        action: 'The unit was edited without a restart — reconcile before trusting the slot budget.',
      })
    }
  }

  /* ------------------------------------------------------------- memory -- */
  for (const m of machines) {
    for (const meter of m.meters) {
      if (meter.warn) {
        findings.push({
          verdict: 'critical',
          cause: `${m.spec.title} ${meter.title} is spilling into host memory`,
          action: 'The pools are no longer independent; a co-resident model is at risk. Unload one.',
        })
      }
    }
  }

  /* ------------------------------------------------------------ thermal -- */
  for (const m of machines) findings.push(...thermal(m.temps, m.spec.title))

  /* -------------------------------------------------------------- route -- */
  if (route?.pinned && route.serving && route.pinned !== route.serving) {
    findings.push({
      verdict: 'warn',
      cause: `Route is pinned to ${route.pinned} but ${route.serving} is answering`,
      action: 'Reconcile the pin, or clear it and let auto choose.',
    })
  }

  /* ------------------------------------------------------------ context -- */
  const residents = machines.reduce((n, m) => n + m.residents.length, 0)
  const queued = reachable.reduce((n, u) => n + (u.requests_deferred ?? 0), 0)
  context.push(
    `${residents} model${residents === 1 ? '' : 's'} resident of ${(servers ?? []).length} registered`
  )
  context.push(queued > 0 ? `${queued} request${queued === 1 ? '' : 's'} queued` : 'nothing queued')

  // The tightest real thermal margin across the fleet, named. A box whose
  // sensors report no limits contributes nothing here rather than a false pass.
  const ranked = byHeadroom(
    machines.flatMap((m) =>
      (m.temps?.status === 'available' ? m.temps.sensors : []).map((s) => ({ ...s, host: m.spec.title }))
    )
  ).filter((s) => headroomC(s) !== null)
  if (ranked.length) {
    const t = ranked[0]
    context.push(`tightest thermal margin ${(headroomC(t) as number).toFixed(1)} °C (${t.host} ${t.label})`)
  }

  // Free VRAM on the tightest device pool: the question "will another model
  // fit?" is asked constantly and answered nowhere on the page.
  const pools = machines.flatMap((m) => m.meters.map((x) => ({ ...x, host: m.spec.title })))
    .filter((p) => p.key !== 'system' && Number.isFinite(p.free_gib))
  if (pools.length) {
    const tightest = pools.reduce((a, b) => (a.free_gib <= b.free_gib ? a : b))
    context.push(`${tightest.free_gib.toFixed(1)} GiB free on ${tightest.host} ${tightest.title}`)
  }

  const verdict = findings.reduce<Verdict>((acc, f) => worst(acc, f.verdict), 'ok')
  return {
    verdict,
    headline: HEADLINE[verdict],
    findings: [...findings].sort((a, b) => RANK[a.verdict] - RANK[b.verdict]),
    context,
  }
}
