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

/**
 * One knob of a model's launch configuration — device placement, context, KV
 * type, drafter, slots. `source` is what makes this honest: the same value
 * means something different depending on whether ARIA set it, Ben wrote a
 * drop-in by hand, or the deployment's serve.sh simply defaults to it, and
 * only the first is ARIA's to clear.
 */
type LaunchParam = {
  name: string
  env: string
  label: string
  kind: 'int' | 'enum' | 'path' | 'str'
  description?: string
  declared_default?: string | null
  choices?: { value: string; description?: string }[]
  value?: string | null
  source?: 'aria_override' | 'unit_dropin' | 'script_default' | 'declared_default' | 'unset'
}

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
  // Where it runs. Two GPUs with separate memory means entries in different
  // pools can be resident simultaneously — the fleet list is not a queue.
  memory_pool?: string
  also_uses?: string[]
  devices?: string[]
  deployment?: string | null
  pool_used_gib?: number | null
  pool_total_gib?: number | null
  pool_spilling?: boolean
  // How it loads. An empty list means the configuration is frozen in the
  // deployment's compose file or unit and cannot be chosen from here.
  parameters?: LaunchParam[]
  aria_overrides?: Record<string, string>
  served_ctx?: number | null
  slots?: number | null
}

const POOL_LABELS: Record<string, string> = {
  'halo-gtt': 'Strix Halo',
  'r9700-vram': 'R9700',
  'host-ram': 'CPU',
  remote: 'remote',
}

const SOURCE_LABELS: Record<string, string> = {
  aria_override: 'set here',
  unit_dropin: 'unit drop-in',
  script_default: 'script default',
  declared_default: 'default',
  unset: 'unset',
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

/**
 * Live slot occupancy for one running server
 * (GET /infrastructure/model-servers/utilization).
 *
 * The fleet list above reports how many slots a server *should* have (parsed
 * from its unit file); this reports how many are busy right now. Note that
 * every field can be null: a server launched without `--metrics` exposes
 * `/slots` but not `/metrics`, so occupancy is readable while queue depth and
 * throughput are not. Null means unknown here, never zero.
 */
type Util = {
  slug: string
  reachable?: boolean
  busy_slots?: number | null
  total_slots?: number | null
  free_slots?: number | null
  slot_utilisation?: number | null
  ctx_per_slot?: number | null
  declared_slots?: number | null
  declared_ctx_per_slot?: number | null
  saturated?: boolean | null
  requests_processing?: number | null
  requests_deferred?: number | null
  prompt_tokens_per_second?: number | null
  predicted_tokens_per_second?: number | null
  metrics_available?: boolean
  metrics_hint?: string | null
}

/**
 * A non-LLM service (GET /infrastructure/services) — mongod, embeddings,
 * hermes-gateway, signal-cli, samba, ...
 *
 * Deliberately a separate registry from the model servers above, and the
 * difference that matters is `expected_state`: a stopped model server is
 * NORMAL (they are mutually RAM-exclusive), whereas a stopped `always_up`
 * service is an incident. `healthy` already encodes that rule server-side, so
 * this panel renders the verdict rather than re-deriving it.
 */
type Service = {
  slug: string
  description?: string
  kind?: string
  state?: string
  expected_state?: 'always_up' | 'on_demand'
  healthy?: boolean
  port?: number | null
  manageable?: boolean
  needs_review?: boolean
  notes?: string | null
  unit?: string | null
  container?: string | null
}

export default function OperatePage() {
  const [servers, setServers] = useState<Server[]>([])
  const [util, setUtil] = useState<Util[]>([])
  const [services, setServices] = useState<Service[]>([])
  const [runs, setRuns] = useState<BenchRun[]>([])
  const [route, setRoute] = useState<Route | null>(null)
  const [sel, setSel] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const [data, r, svc, u] = await Promise.all([
        apiClient.listModelServers(),
        apiClient.getLlmRoute().catch(() => null),
        // Tolerated separately: the services registry going quiet must not
        // blank the model-server view, which is the page's primary job.
        apiClient.listServices().catch(() => null),
        // Same reasoning, and more so: this one reaches out to each running
        // llama.cpp, so a wedged server must degrade to "no slot data" rather
        // than take the whole page down with it.
        apiClient.modelServerUtilization().catch(() => null),
      ])
      const list: Server[] = data?.servers ?? []
      // Ranked by memory weight: weight is what decides what you can run, so it
      // is the only ordering that helps you make the next decision.
      list.sort((a, b) => (b.resident_gib_estimate ?? 0) - (a.resident_gib_estimate ?? 0))
      setServers(list)
      setRoute(r)
      setUtil((u?.servers as Util[]) ?? [])
      // Unhealthy first — this panel exists to answer "is anything wrong?",
      // and a 19-row alphabetical list buries the one row that matters.
      // Then always_up before on_demand, then by slug for stability.
      setServices(
        [...((svc?.services as Service[]) ?? [])].sort((a, b) => {
          if (!!a.healthy !== !!b.healthy) return a.healthy ? 1 : -1
          if (a.expected_state !== b.expected_state)
            return a.expected_state === 'always_up' ? -1 : 1
          return a.slug.localeCompare(b.slug)
        }),
      )
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

  /**
   * The GPU pools, deduplicated from the server rows.
   *
   * Read from the rows rather than fetched separately so this survives the
   * devices endpoint being unavailable — and the CPU pool is dropped because
   * host RAM pressure is not what decides whether a model fits here.
   */
  const pools = useMemo(() => {
    const out = new Map<string, { pool: string; used_gib: number; total_gib: number; spilling?: boolean }>()
    for (const s of servers) {
      if (!s.memory_pool || s.memory_pool === 'host-ram' || s.memory_pool === 'remote') continue
      if (s.pool_total_gib == null || out.has(s.memory_pool)) continue
      out.set(s.memory_pool, {
        pool: s.memory_pool,
        used_gib: s.pool_used_gib ?? 0,
        total_gib: s.pool_total_gib,
        spilling: s.pool_spilling,
      })
    }
    return [...out.values()]
  }, [servers])

  async function act(
    kind: 'start' | 'stop' | 'sleep',
    slug: string,
    overrides?: Record<string, string> | null,
  ) {
    setBusy(slug + kind)
    setError(null)
    try {
      if (kind === 'start') await apiClient.startModelServer(slug, false, overrides)
      if (kind === 'stop') await apiClient.stopModelServer(slug)
      if (kind === 'sleep') await apiClient.sleepModelServer(slug)
      await load()
    } catch (e: any) {
      setError(typeof e?.message === 'string' ? e.message : 'Action failed')
    } finally {
      setBusy(null)
    }
  }

  async function actService(kind: 'start' | 'stop', slug: string) {
    setBusy(slug + kind)
    setError(null)
    try {
      if (kind === 'start') await apiClient.startService(slug)
      else await apiClient.stopService(slug)
      await load()
    } catch (e: any) {
      setError(typeof e?.message === 'string' ? e.message : 'Action failed')
    } finally {
      setBusy(null)
    }
  }

  const unhealthy = services.filter((s) => !s.healthy)
  const needsReview = services.filter((s) => s.needs_review).length

  // The status bar tracks the server that is actually answering, since that is
  // the one whose slots you are competing for.
  const servingUtil = util.find((u) => u.slug === route?.serving && u.reachable)

  const status = (
    <>
      <StatusStat label="SERVICES">
        {unhealthy.length > 0 ? `${unhealthy.length} down` : `${services.length} ok`}
      </StatusStat>
      {/* Per device, not per box: this machine has two GPUs with separate
          memory, so one combined figure would hide which of them is actually
          full — and a model on the R9700 does not compete with one on the
          Halo. */}
      {pools.length > 0 ? (
        pools.map((p) => (
          <StatusStat key={p.pool} label={POOL_LABELS[p.pool] ?? p.pool}>
            {p.used_gib.toFixed(1)} / {p.total_gib.toFixed(1)} GiB
            {p.spilling ? ' · spilling' : ''}
          </StatusStat>
        ))
      ) : (
        <StatusStat label="GTT">
          {gtt.used.toFixed(1)} / {gtt.total.toFixed(1)} GiB
        </StatusStat>
      )}
      <StatusStat label="RESIDENT">
        {onbox.filter((s) => normalizeState(s.state) === 'running').length} of {onbox.length}
      </StatusStat>
      <StatusStat label="SERVING">
        {route?.serving ?? '—'}
        {route?.pinned ? ' · pinned' : ''}
      </StatusStat>
      <StatusStat label="SLOTS">
        {servingUtil
          ? `${servingUtil.busy_slots ?? '?'} / ${servingUtil.total_slots ?? '?'}${
              servingUtil.saturated ? ' · queuing' : ''
            }`
          : '—'}
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
          <LaunchPanel server={selected} busy={busy} onAct={act} />
          <SlotsPanel util={util} servers={servers} />
          <ServingPanel route={route} busy={busy} onPin={setPin} />
          <BenchPanel server={selected} runs={runs} />
        </div>

        {/* -------------------------------------------------------- machine */}
        <div className="min-w-0">
          <BudgetPanel server={selected} gtt={gtt} residentGpu={residentGpu} />
          <ServicesPanel
            services={services}
            busy={busy}
            onAct={actService}
            needsReview={needsReview}
          />
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
          { k: 'Device', v: server.devices?.join(' + ') || server.backend_device || '—' },
          // Which pool it spends. Two models in DIFFERENT pools can be resident
          // at the same time — that is the point of the second card, and the
          // fleet list alone does not say it.
          {
            k: 'Memory',
            v: [
              POOL_LABELS[server.memory_pool ?? ''] ?? server.memory_pool ?? '—',
              ...(server.also_uses ?? []).map((p) => `+ ${POOL_LABELS[p] ?? p}`),
              server.pool_total_gib
                ? `· ${(server.pool_used_gib ?? 0).toFixed(0)}/${server.pool_total_gib.toFixed(0)} GiB used`
                : '',
            ]
              .filter(Boolean)
              .join(' '),
          },
          { k: 'Serves', v: server.served_ctx ? `${server.served_ctx.toLocaleString()} ctx x ${server.slots ?? 1}` : '—' },
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
 * Launch configuration — how the selected model loads, not just whether it does.
 *
 * The knobs are the deployment's own env vars (its serve.sh is already written
 * as `VAR="${VAR:-default}"`), so choosing here does exactly what Ben's
 * hand-written drop-ins do — ARIA writes one more drop-in that sorts last. That
 * matters for what this panel must show: a value can come from ARIA, from a
 * hand-written drop-in, or from the script itself, and only the first is
 * ARIA's to clear. Hence the `source` chip on every row rather than a bare box.
 *
 * "Start with defaults" is a real, distinct action, not a reset button: it
 * clears ARIA's overrides AND starts, so a context size set for one experiment
 * cannot silently outlive it.
 */
function LaunchPanel({
  server,
  busy,
  onAct,
}: {
  server: Server | null
  busy: string | null
  onAct: (
    k: 'start' | 'stop' | 'sleep',
    slug: string,
    overrides?: Record<string, string> | null,
  ) => void
}) {
  const params = server?.parameters ?? []
  const [draft, setDraft] = useState<Record<string, string>>({})

  // Reset the form when the selection changes, so a value typed against one
  // model is never submitted against another.
  useEffect(() => {
    setDraft({})
  }, [server?.slug])

  if (!server || server.onbox === false) return null

  const st = normalizeState(server.state)
  const running = st === 'running'
  const effective = (p: LaunchParam) => draft[p.name] ?? p.value ?? ''
  const changed = params.some((p) => draft[p.name] !== undefined && draft[p.name] !== p.value)
  const overrides = Object.fromEntries(
    params
      .filter((p) => effective(p) !== '' && (draft[p.name] !== undefined || p.source === 'aria_override'))
      .map((p) => [p.name, effective(p)]),
  )

  if (params.length === 0) {
    return (
      <Card title="Launch configuration">
        <EmptyState>
          Frozen in {server.deployment ? `${server.deployment}'s ` : ''}
          {server.model_file || server.port ? 'compose file' : 'unit'} — this model has no
          selectable launch parameters. Edit that file to change how it loads.
        </EmptyState>
      </Card>
    )
  }

  return (
    <Card
      title="Launch configuration"
      hint={running ? '· applies on next start' : `· ${params.length} parameters`}
    >
      <div className="flex flex-col gap-2.5">
        {params.map((p) => (
          <div key={p.name} className="grid grid-cols-[minmax(120px,150px)_1fr] items-start gap-2.5">
            <div className="pt-1">
              <div className="font-sans text-[12px] text-ink">{p.label}</div>
              <div className="font-mono text-[10px] text-ink-faint">{p.env}</div>
            </div>
            <div className="min-w-0">
              {p.kind === 'enum' && p.choices?.length ? (
                <select
                  value={effective(p)}
                  onChange={(e) => setDraft((d) => ({ ...d, [p.name]: e.target.value }))}
                  className="w-full rounded border border-line bg-panel-2 px-2 py-1 font-mono text-[11px] text-ink focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
                >
                  {p.choices.map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.value}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  value={effective(p)}
                  inputMode={p.kind === 'int' ? 'numeric' : 'text'}
                  list={p.choices?.length ? `${server.slug}-${p.name}-opts` : undefined}
                  onChange={(e) => setDraft((d) => ({ ...d, [p.name]: e.target.value }))}
                  className="w-full rounded border border-line bg-panel-2 px-2 py-1 font-mono text-[11px] text-ink focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
                />
              )}
              {p.choices?.length && p.kind !== 'enum' ? (
                <datalist id={`${server.slug}-${p.name}-opts`}>
                  {p.choices.map((c) => (
                    <option key={c.value} value={c.value} label={c.description} />
                  ))}
                </datalist>
              ) : null}
              <div className="mt-0.5 flex flex-wrap items-baseline gap-x-2">
                <span
                  className={`font-sans text-[10px] uppercase tracking-wide ${
                    p.source === 'aria_override' ? 'text-accent' : 'text-ink-faint'
                  }`}
                >
                  {SOURCE_LABELS[p.source ?? 'unset'] ?? p.source}
                </span>
                {/* The chosen option's own explanation — these carry measured
                    trade-offs (what fits, what costs decode), so they are worth
                    the row rather than being hidden in a tooltip. */}
                {p.choices?.find((c) => c.value === effective(p))?.description && (
                  <span className="font-sans text-[10px] text-ink-dim">
                    {p.choices.find((c) => c.value === effective(p))!.description}
                  </span>
                )}
              </div>
              {p.description && (
                <p className="mt-1 max-w-[70ch] font-sans text-[10px] leading-relaxed text-ink-faint">
                  {p.description}
                </p>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="mt-3.5 flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          disabled={running || server.startable === false}
          busy={busy === server.slug + 'start'}
          onClick={() => onAct('start', server.slug, overrides)}
          title={
            running
              ? 'Stop it first — llama.cpp cannot change these without a reload'
              : 'Start with the settings above'
          }
        >
          Start with these settings
        </Button>
        <Button
          disabled={running || server.startable === false}
          busy={busy === server.slug + 'start'}
          onClick={() => {
            setDraft({})
            onAct('start', server.slug, null)
          }}
          title="Start with the deployment's own defaults, clearing any override ARIA set"
        >
          Start with defaults
        </Button>
        {changed && <span className="font-sans text-[11px] text-ink-dim">unsaved changes</span>}
      </div>

      {running && (
        <Notice tone="warn">
          Running with the configuration shown. These are load-time settings — stop and start
          again to apply a change.
        </Notice>
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

/**
 * Slot occupancy — the runtime counterpart to the memory budget.
 *
 * Memory decides which models can be loaded at all; slots decide whether the
 * loaded one still feels fast. The design of this box is one slot per consumer
 * (Hermes, the pi coding agent, its sub-agents, ARIA's workers), each holding
 * its own prefix permanently, so:
 *
 * - Every slot busy is FINE and expected. Utilisation is not the alarm.
 * - `saturated` IS the alarm: requests are queuing, and a queued request lands
 *   in whichever slot frees first rather than the one holding its prefix. That
 *   is how a warm cache silently decays into a cold prefill every turn, which
 *   reads to a human as "the model got slow" with nothing in any log.
 *
 * Unknowns render as unknown. A server launched without `--metrics` serves
 * `/slots` but not `/metrics`, so occupancy shows while queue depth and
 * throughput stay blank — deliberately not drawn as zeroes, which would read as
 * a confident "nothing is queuing".
 */
function SlotsPanel({ util, servers }: { util: Util[]; servers: Server[] }) {
  const rows = util.filter((u) => u.reachable)
  if (rows.length === 0) return null

  return (
    <Card title="Slots" className="mt-3.5" hint="· live, per running server">
      <div className="flex flex-col gap-3.5">
        {rows.map((u) => {
          const total = u.total_slots ?? 0
          const busy = u.busy_slots ?? 0
          const pct = total > 0 ? (busy / total) * 100 : 0
          // Declared comes from the unit file, live from the server itself, so
          // they diverge exactly when a unit was edited without a restart.
          const drift =
            (u.declared_slots != null && u.total_slots != null && u.declared_slots !== u.total_slots) ||
            (u.declared_ctx_per_slot != null &&
              u.ctx_per_slot != null &&
              u.declared_ctx_per_slot !== u.ctx_per_slot)
          const cpu = !servers.find((s) => s.slug === u.slug && isGpu(s))

          return (
            <div key={u.slug}>
              <div className="mb-1.5 flex items-baseline justify-between gap-2.5">
                <span className="truncate text-xs text-ink" title={u.slug}>
                  {u.slug}
                </span>
                <span className="tnum shrink-0 text-[11px] text-ink-dim">
                  {busy} / {total || '?'} busy
                  {u.ctx_per_slot ? ` · ${Math.round(u.ctx_per_slot / 1024)}K each` : ''}
                </span>
              </div>

              <Meter
                segments={[
                  {
                    key: 'busy',
                    pct,
                    // Full is normal; queuing is not. Colour follows the alarm,
                    // not the fill level.
                    color: u.saturated ? 'bg-gone' : cpu ? 'bg-ink-faint' : 'bg-live',
                  },
                ]}
                left={
                  u.metrics_available && u.predicted_tokens_per_second != null
                    ? `${u.predicted_tokens_per_second.toFixed(1)} tok/s out · ${(
                        u.prompt_tokens_per_second ?? 0
                      ).toFixed(0)} tok/s prefill`
                    : 'throughput unavailable'
                }
                right={
                  u.requests_deferred != null && u.requests_deferred > 0
                    ? `${u.requests_deferred} queued`
                    : `${u.free_slots ?? Math.max(0, total - busy)} free`
                }
              />

              {u.saturated && (
                <Notice tone="warn">
                  <b>Requests are queuing.</b> {u.requests_deferred} deferred — a queued request takes
                  whichever slot frees first, not the one holding its prefix, so expect cold prefills
                  until this clears. Reduce concurrent sessions, or raise <code>-np</code> in the unit
                  (each extra slot costs a full <code>-c</code> worth of KV).
                </Notice>
              )}

              {drift && (
                <Notice tone="warn">
                  <b>Unit and running server disagree.</b> Launch file declares{' '}
                  {u.declared_slots ?? '?'} × {u.declared_ctx_per_slot ?? '?'}, the server reports{' '}
                  {u.total_slots ?? '?'} × {u.ctx_per_slot ?? '?'} — it was edited but never restarted.
                </Notice>
              )}

              {u.metrics_available === false && (
                <p className="mt-1.5 font-sans text-[10px] leading-relaxed text-ink-faint">
                  {u.metrics_hint ||
                    'Started without --metrics: slot counts are live, queue depth and throughput are unreadable.'}
                </p>
              )}
            </div>
          )
        })}
      </div>
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

/**
 * Services — the non-LLM half of "what is running on this box".
 *
 * The whole reason this is a separate registry from the fleet above is that
 * "stopped" means opposite things on each side: a stopped model server is
 * normal (they are mutually RAM-exclusive and only one big one fits), while a
 * stopped mongod is an incident. That distinction lives in `expected_state`,
 * and the server has already applied it to `healthy` — so this renders the
 * verdict rather than second-guessing it from `state`.
 */
function ServicesPanel({
  services,
  busy,
  onAct,
  needsReview,
}: {
  services: Service[]
  busy: string | null
  onAct: (kind: 'start' | 'stop', slug: string) => void
  needsReview: number
}) {
  const down = services.filter((s) => !s.healthy)

  return (
    <Card
      title="Services"
      className="mt-3.5"
      hint={services.length ? `· ${services.length}` : undefined}
      bodyClassName=""
    >
      {services.length === 0 ? (
        <div className="p-3.5">
          <EmptyState>No service registry — is aria-api up to date?</EmptyState>
        </div>
      ) : (
        <>
          {down.length > 0 && (
            <div className="border-b border-line p-3.5">
              <Notice tone="warn">
                <b>
                  {down.length} service{down.length === 1 ? '' : 's'} down.
                </b>{' '}
                {down.map((s) => s.slug).join(', ')} — expected to be running.
              </Notice>
            </div>
          )}

          <ul className="m-0 list-none p-0">
            {services.map((s) => {
              const st = normalizeState(s.state)
              const stopped = st !== 'running'
              const acting = busy === s.slug + 'start' || busy === s.slug + 'stop'
              return (
                <li
                  key={s.slug}
                  className="grid grid-cols-[8px_1fr_auto] items-center gap-2.5 border-b border-line px-2.5 py-2 last:border-b-0"
                >
                  {/* An on_demand service that is merely stopped is fine, so it
                      must not show the same red dot as a downed mongod. */}
                  <StatusDot state={s.healthy ? (stopped ? 'external' : 'running') : 'absent'} />
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate text-xs text-ink" title={s.description || s.slug}>
                        {s.slug}
                      </span>
                      {s.needs_review && (
                        <span
                          className="shrink-0 text-[10px] text-idle"
                          title="expected_state was inferred, not confirmed — worth a look"
                        >
                          ⚠
                        </span>
                      )}
                    </div>
                    <div className="truncate text-[10px] text-ink-faint">
                      {s.state}
                      {s.expected_state === 'on_demand' ? ' · on demand' : ''}
                      {s.port ? ` · :${s.port}` : ''}
                    </div>
                  </div>
                  {s.manageable === false ? (
                    <span
                      className="shrink-0 text-[10px] text-ink-faint"
                      title={s.notes || 'ARIA must not start/stop this one'}
                    >
                      locked
                    </span>
                  ) : (
                    <Button
                      busy={acting}
                      onClick={() => onAct(stopped ? 'start' : 'stop', s.slug)}
                      className="shrink-0"
                    >
                      {stopped ? 'start' : 'stop'}
                    </Button>
                  )}
                </li>
              )
            })}
          </ul>

          {needsReview > 0 && (
            <div className="border-t border-line px-3.5 py-2 text-[10px] text-ink-faint">
              {needsReview} marked ⚠ — their expected_state was inferred from
              observed state, not confirmed. Until then an outage on those will
              not alert.
            </div>
          )}
        </>
      )}
    </Card>
  )
}
