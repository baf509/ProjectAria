/**
 * Operate — the machine.
 *
 * Everything needed to load, watch and benchmark a model on one surface. These
 * were previously split across /dashboard and /dashboard/benchmarks with no
 * shared state, so the load -> bench -> compare -> swap loop meant navigating
 * away and losing context.
 *
 * The spine of the screen is the memory budget, because the governing fact of
 * this box is that the large models are mutually exclusive: 124 GiB of unified
 * memory, and DS4 alone wants 86.5 of it. The old UI buried that in an
 * `exclusive_with` array; here it is a meter that previews the swap.
 */
'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { AppShell, StatusStat } from '@/components/AppShell'
import {
  Button,
  Card,
  EmptyState,
  KeyValue,
  Meter,
  Notice,
  ScrollX,
  Sparkline,
  StateChip,
  StatusDot,
  normalizeState,
  type ServerState,
} from '@/components/ui'
import { apiClient } from '@/lib/api-client'
import { benchmarksApi, type BenchRun } from '@/lib/api-client-benchmarks'

type Server = {
  slug: string
  description?: string
  state?: string
  port?: number | null
  model_file?: string | null
  runtime_repo?: string | null
  runtime_ref?: string | null
  backend_device?: string | null
  resident_gib_estimate?: number | null
  exclusive_with?: string[]
  onbox?: boolean
  startable?: boolean
  not_startable_reason?: string | null
  consumers_note?: string | null
  endpoints?: { local?: string; tailnet?: string }
  gtt_used_gib?: number
  gtt_total_gib?: number
}

/** CPU-only servers cost no GTT, which is why they can coexist with a big model. */
const isGpu = (s: Server) => !/cpu/i.test(s.backend_device || '') && (s.resident_gib_estimate ?? 0) > 0

/** Who currently answers as "the local model" (GET /infrastructure/llm-route). */
type Route = {
  pinned?: string | null
  serving?: string | null
  reason?: string
  loaded?: { slug: string; resident_gib?: number | null }[]
}

export default function OperatePage() {
  const [servers, setServers] = useState<Server[]>([])
  const [runs, setRuns] = useState<BenchRun[]>([])
  const [route, setRoute] = useState<Route | null>(null)
  const [sel, setSel] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const [data, r] = await Promise.all([
        apiClient.listModelServers(),
        apiClient.getLlmRoute().catch(() => null),
      ])
      const list: Server[] = data?.servers ?? []
      // Ranked by memory weight: weight is what decides what you can run, so it
      // is the only ordering that helps you make the next decision.
      list.sort((a, b) => (b.resident_gib_estimate ?? 0) - (a.resident_gib_estimate ?? 0))
      setServers(list)
      setRoute(r)
      setSel((cur) => cur ?? list.find((s) => normalizeState(s.state) === 'running')?.slug ?? list[0]?.slug ?? null)
      setError(null)
    } catch (e: any) {
      setError(e?.message || 'Could not reach ARIA')
    } finally {
      setLoading(false)
    }
  }, [])

  const setPin = useCallback(
    async (slug: string | null) => {
      setBusy('route')
      setError(null)
      try {
        setRoute(await apiClient.setLlmRoute(slug))
      } catch (e: any) {
        setError(typeof e?.message === 'string' ? e.message : 'Could not change the route')
      } finally {
        setBusy(null)
      }
    },
    [],
  )

  useEffect(() => {
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [load])

  useEffect(() => {
    benchmarksApi.listRuns(25).then(setRuns).catch(() => setRuns([]))
  }, [])

  const selected = servers.find((s) => s.slug === sel) ?? null
  const onbox = servers.filter((s) => s.onbox !== false)
  const residentGpu = onbox.filter((s) => normalizeState(s.state) === 'running' && isGpu(s))

  const gtt = useMemo(() => {
    const withGtt = servers.find((s) => s.gtt_total_gib)
    const total = withGtt?.gtt_total_gib ?? 124
    const used =
      withGtt?.gtt_used_gib ?? residentGpu.reduce((a, b) => a + (b.resident_gib_estimate ?? 0), 0)
    return { used, total }
  }, [servers, residentGpu])

  async function act(kind: 'start' | 'stop' | 'sleep', slug: string) {
    setBusy(slug + kind)
    setError(null)
    try {
      if (kind === 'start') await apiClient.startModelServer(slug)
      if (kind === 'stop') await apiClient.stopModelServer(slug)
      if (kind === 'sleep') await apiClient.sleepModelServer(slug)
      await load()
    } catch (e: any) {
      setError(typeof e?.message === 'string' ? e.message : 'Action failed')
    } finally {
      setBusy(null)
    }
  }

  const status = (
    <>
      <StatusStat label="GTT">
        {gtt.used.toFixed(1)} / {gtt.total.toFixed(1)} GiB
      </StatusStat>
      <StatusStat label="RESIDENT">
        {onbox.filter((s) => normalizeState(s.state) === 'running').length} of {onbox.length}
      </StatusStat>
      <StatusStat label="SERVING">
        {route?.serving ?? '—'}
        {route?.pinned ? ' · pinned' : ''}
      </StatusStat>
      <StatusStat label="HOST">corsair-ai · gfx1151</StatusStat>
    </>
  )

  return (
    <AppShell area="Operate" status={status}>
      {error && (
        <div className="mb-3.5 rounded border border-gone bg-gone/10 px-3.5 py-2.5 font-sans text-xs text-ink">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-3.5 md:grid-cols-[minmax(220px,0.85fr)_1.6fr] xl:grid-cols-[minmax(240px,0.8fr)_1.7fr_minmax(240px,0.9fr)]">
        {/* ---------------------------------------------------------- fleet */}
        <div className="min-w-0">
          <Card title="Fleet" hint={loading ? 'loading…' : `· ${onbox.length} · by memory weight`} bodyClassName="">
            <ul className="m-0 list-none p-0">
              {servers.map((s) => {
                const st = normalizeState(s.state)
                const active = s.slug === sel
                return (
                  <li key={s.slug} className="border-b border-line last:border-b-0">
                    <button
                      onClick={() => setSel(s.slug)}
                      aria-current={active}
                      className={`grid w-full grid-cols-[8px_1fr_auto] items-center gap-2.5 border-l-2 px-2.5 py-2 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${
                        active ? 'border-accent bg-panel-2' : 'border-transparent hover:bg-panel-2'
                      }`}
                    >
                      <StatusDot state={st} />
                      <span
                        className={`truncate text-xs ${active ? 'text-accent' : 'text-ink'}`}
                        title={s.slug}
                      >
                        {s.slug}
                      </span>
                      <span className={`tnum text-[11px] ${isGpu(s) ? 'text-ink-dim' : 'text-ink-faint'}`}>
                        {s.onbox === false ? 'off-box' : isGpu(s) ? (s.resident_gib_estimate ?? 0).toFixed(1) : 'cpu'}
                      </span>
                    </button>
                  </li>
                )
              })}
            </ul>
          </Card>
        </div>

        {/* --------------------------------------------------------- detail */}
        <div className="min-w-0">
          <Detail
            server={selected}
            busy={busy}
            onAct={act}
            route={route}
            onPin={setPin}
          />
          <ServingPanel route={route} busy={busy} onPin={setPin} />
          <BenchPanel server={selected} runs={runs} />
        </div>

        {/* -------------------------------------------------------- machine */}
        <div className="min-w-0">
          <BudgetPanel server={selected} gtt={gtt} residentGpu={residentGpu} />
        </div>
      </div>
    </AppShell>
  )
}

/* ------------------------------------------------------------------ pieces */

function Detail({
  server,
  busy,
  onAct,
  route,
  onPin,
}: {
  server: Server | null
  busy: string | null
  onAct: (k: 'start' | 'stop' | 'sleep', slug: string) => void
  route: Route | null
  onPin: (slug: string | null) => void
}) {
  if (!server)
    return (
      <Card title="Model">
        <EmptyState>Select a model from the fleet.</EmptyState>
      </Card>
    )

  const st: ServerState = server.onbox === false ? 'external' : normalizeState(server.state)
  const running = st === 'running'
  const endpoint = server.endpoints?.tailnet || server.endpoints?.local || '—'

  return (
    <Card title="Model">
      <div className="flex flex-wrap items-baseline gap-x-3.5 gap-y-2">
        <h3 className="m-0 text-base font-semibold tracking-tight">{server.slug}</h3>
        <StateChip state={st} />
        {route?.serving === server.slug && (
          <span className="rounded border border-accent px-1.5 py-0.5 font-sans text-[10px] uppercase tracking-wide text-accent">
            serving {route.pinned === server.slug ? '· pinned' : '· auto'}
          </span>
        )}
      </div>

      {server.description && (
        <p className="mt-2.5 max-w-[70ch] font-sans text-[13px] leading-relaxed text-ink-dim">
          {server.description}
        </p>
      )}

      <KeyValue
        items={[
          { k: 'Resident', v: isGpu(server) ? `${(server.resident_gib_estimate ?? 0).toFixed(1)} GiB` : 'CPU only' },
          { k: 'Port', v: server.port ?? '—' },
          { k: 'Device', v: server.backend_device ?? '—' },
          { k: 'Endpoint', v: endpoint, title: endpoint },
          { k: 'Runtime', v: server.runtime_ref ?? server.runtime_repo ?? '—', title: server.runtime_ref ?? '' },
          { k: 'Weights', v: server.model_file ?? '—', title: server.model_file ?? '' },
        ]}
      />

      {server.consumers_note && (
        <p className="mt-2.5 font-sans text-[11px] text-ink-faint">Consumers: {server.consumers_note}</p>
      )}

      <div className="mt-3.5 flex flex-wrap gap-2">
        <Button
          variant="primary"
          disabled={running || server.startable === false || server.onbox === false}
          busy={busy === server.slug + 'start'}
          onClick={() => onAct('start', server.slug)}
        >
          {running ? 'Running' : 'Start'}
        </Button>
        <Button
          disabled={!running || server.onbox === false}
          busy={busy === server.slug + 'stop'}
          onClick={() => onAct('stop', server.slug)}
        >
          Stop
        </Button>
        <Button
          disabled={server.onbox !== false}
          busy={busy === server.slug + 'sleep'}
          onClick={() => onAct('sleep', server.slug)}
        >
          Sleep host
        </Button>
        {route?.pinned === server.slug ? (
          <Button busy={busy === 'route'} onClick={() => onPin(null)}>
            Unpin
          </Button>
        ) : (
          <Button
            disabled={!running || server.onbox === false}
            busy={busy === 'route'}
            onClick={() => onPin(server.slug)}
            title="Make this the local model ARIA and Hermes talk to"
          >
            Serve this
          </Button>
        )}
      </div>

      {server.startable === false && server.not_startable_reason && (
        <Notice tone="warn">{server.not_startable_reason}</Notice>
      )}
    </Card>
  )
}

/**
 * The local-model route.
 *
 * This exists because more than one model can be resident at once, so "the
 * local model" is not self-evident from the fleet list. Both ARIA (LLAMACPP_URL)
 * and Hermes point at ARIA's /llm/v1 passthrough, so whatever this panel says is
 * serving is what they are both actually using — the one place to look instead
 * of inferring it from a completion.
 */
function ServingPanel({
  route,
  busy,
  onPin,
}: {
  route: Route | null
  busy: string | null
  onPin: (slug: string | null) => void
}) {
  if (!route) return null
  const loaded = route.loaded ?? []

  return (
    <Card title="Local model route" hint="· ARIA + Hermes follow this">
      {loaded.length === 0 ? (
        <EmptyState>Nothing is loaded — start a model and both ARIA and Hermes follow it.</EmptyState>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => onPin(null)}
              disabled={busy === 'route'}
              aria-pressed={!route.pinned}
              className={`rounded border px-2 py-1 font-sans text-[11px] transition-colors ${
                !route.pinned ? 'border-accent text-accent' : 'border-line text-ink-dim hover:bg-panel-2'
              }`}
            >
              Auto
            </button>
            {loaded.map((m) => (
              <button
                key={m.slug}
                onClick={() => onPin(m.slug)}
                disabled={busy === 'route'}
                aria-pressed={route.pinned === m.slug}
                className={`rounded border px-2 py-1 font-sans text-[11px] transition-colors ${
                  route.pinned === m.slug
                    ? 'border-accent text-accent'
                    : 'border-line text-ink-dim hover:bg-panel-2'
                }`}
              >
                {m.slug}
              </button>
            ))}
          </div>
          <p className="mt-2.5 font-sans text-[11px] text-ink-faint">
            Serving <span className="text-ink-dim">{route.serving ?? '—'}</span> — {route.reason}
          </p>
          {loaded.length > 1 && !route.pinned && (
            <Notice tone="warn">
              {loaded.length} models are loaded. Auto picks the largest; pin one to make the choice
              explicit. Callers can also name a model directly (Hermes: <code>/model {loaded[0].slug}</code>).
            </Notice>
          )}
        </>
      )}
    </Card>
  )
}

function BenchPanel({ server, runs }: { server: Server | null; runs: BenchRun[] }) {
  if (!server) return null

  // Match a run to this server by port appearing in the target's base_url, the
  // only identifier the two systems reliably share.
  const mine = runs.filter((r) =>
    server.port ? r.targets.some((t) => t.includes(String(server.port)) || t.includes(server.slug)) : false,
  )
  const metrics = mine.flatMap((r) => r.metrics ?? [])
  const byMetric = new Map<string, { label: string; value: number }[]>()
  for (const m of metrics) {
    const arr = byMetric.get(m.metric) ?? []
    arr.push({ label: m.benchmark, value: m.value })
    byMetric.set(m.metric, arr)
  }

  return (
    <Card title="Benchmarks" hint="· same surface, no navigation">
      {byMetric.size === 0 ? (
        <EmptyState>
          No benchmark results recorded for {server.slug}. Start it and run one — results land here,
          beside the model, instead of on a separate page.
        </EmptyState>
      ) : (
        <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2">
          {Array.from(byMetric.entries()).map(([metric, rows]) => (
            <div key={metric}>
              <ScrollX>
                <table className="w-full border-collapse text-xs">
                  <thead>
                    <tr>
                      <th className="border-b border-line py-1.5 text-left text-[10px] uppercase tracking-[0.1em] font-medium text-ink-faint">
                        {metric}
                      </th>
                      <th className="border-b border-line py-1.5 text-right text-[10px] uppercase tracking-[0.1em] font-medium text-ink-faint">
                        value
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.slice(0, 8).map((r, i) => (
                      <tr key={i}>
                        <td className="whitespace-nowrap border-b border-line py-1.5 text-ink-dim">{r.label}</td>
                        <td className="tnum whitespace-nowrap border-b border-line py-1.5 text-right">
                          {r.value.toFixed(2)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollX>
              <Sparkline values={rows.slice(0, 8).map((r) => r.value)} label={`${metric} trend`} />
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

function BudgetPanel({
  server,
  gtt,
  residentGpu,
}: {
  server: Server | null
  gtt: { used: number; total: number }
  residentGpu: Server[]
}) {
  const selIsResident = server ? normalizeState(server.state) === 'running' : false
  const selGpu = server ? isGpu(server) : false
  const add = server && selGpu && !selIsResident ? server.resident_gib_estimate ?? 0 : 0
  const projected = gtt.used + add
  const over = projected > gtt.total * 0.92
  const pct = (v: number) => (gtt.total > 0 ? (v / gtt.total) * 100 : 0)

  const segments = [
    { key: 'now', pct: pct(gtt.used), color: 'bg-live' },
    ...(add
      ? [{ key: 'add', pct: pct(Math.min(add, Math.max(0, gtt.total - gtt.used))), color: over ? 'bg-gone' : 'bg-accent' }]
      : []),
  ]

  return (
    <>
      <Card title="Memory budget">
        <Meter
          segments={segments}
          left={
            add
              ? `${gtt.used.toFixed(1)} resident + ${add.toFixed(1)} selected = ${projected.toFixed(1)}`
              : `${gtt.used.toFixed(1)} resident`
          }
          right={`${gtt.total.toFixed(1)} GiB`}
        />
        <div className="mt-2.5 flex flex-wrap gap-x-3.5 gap-y-2 text-[11px] text-ink-dim">
          <span>
            <i className="mr-1.5 inline-block h-2.5 w-2.5 rounded-sm bg-live align-middle" />
            resident
          </span>
          <span>
            <i className="mr-1.5 inline-block h-2.5 w-2.5 rounded-sm bg-accent align-middle" />
            selected
          </span>
          <span>
            <i className="mr-1.5 inline-block h-2.5 w-2.5 rounded-sm bg-track align-middle" />
            free
          </span>
        </div>

        {!server ? null : !selGpu ? (
          <Notice tone="ok">
            <b>No GTT cost.</b> {server.slug} runs on CPU, so it stays up alongside any GPU model —
            which is why it can serve background and cron work while a large model holds the GPU.
          </Notice>
        ) : selIsResident ? (
          <Notice tone="ok">
            <b>Resident now</b>, holding {(server.resident_gib_estimate ?? 0).toFixed(1)} GiB of{' '}
            {gtt.total.toFixed(1)}.
          </Notice>
        ) : server.startable === false ? (
          <Notice tone="warn">
            <b>Cannot start.</b> {server.not_startable_reason || 'Marked not startable.'}
          </Notice>
        ) : over ? (
          <Notice tone="warn">
            <b>Will not fit.</b> Needs {add.toFixed(1)} GiB against {(gtt.total - gtt.used).toFixed(1)} GiB
            free. Stop first:
            <ul className="mt-1.5 list-disc pl-4 text-ink-dim">
              {residentGpu.map((r) => (
                <li key={r.slug}>
                  {r.slug} — frees {(r.resident_gib_estimate ?? 0).toFixed(1)} GiB
                </li>
              ))}
            </ul>
          </Notice>
        ) : (
          <Notice tone="ok">
            <b>Fits.</b> {add.toFixed(1)} GiB needed, {(gtt.total - gtt.used).toFixed(1)} GiB free.
          </Notice>
        )}
      </Card>

      {server && (server.exclusive_with?.length ?? 0) > 0 && (
        <Card title="Mutually exclusive with" className="mt-3.5">
          <ul className="m-0 list-none p-0 text-xs text-ink-dim">
            {server.exclusive_with!.map((x) => (
              <li key={x} className="truncate border-b border-line py-1.5 last:border-b-0" title={x}>
                {x}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </>
  )
}
