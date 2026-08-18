'use client'

/**
 * ARIA - Operate: model-server detail (/operate/servers/[slug])
 *
 * The choices that are not obvious from the JSX:
 *
 * - Start/stop are NOT optimistic. A start can be refused by the registry's
 *   preflight ("preflight rejected: MemAvailable … < 110000000 KiB") and a
 *   load can take ~7 minutes (Radiance streams a 17.7 GiB checkpoint from
 *   disk every cold start). So an action shows a pending state with elapsed
 *   time and the next poll tells the truth.
 *
 * - Action errors are SEPARATE from poll errors. The old page kept one
 *   `error` slot, so the 10s poll succeeding wiped the refusal detail off the
 *   screen before it could be read. Here the refusal stays until dismissed,
 *   superseded by another action, and it offers the force retry inline.
 *
 * - The description gets `Text clamp={3}` + a real expand. This single
 *   paragraph was the measured +40/55px page overflow: registry prose embeds
 *   unbreakable `vault/infrastructure/Analysis/…` paths.
 *
 * - The fit verdict projects onto the server's OWN pool(s), and "stop first"
 *   lists same-pool residents only — the old single-GTT meter told you to
 *   stop a Halo model to fit one on the R9700.
 */
import Link from 'next/link'
import { useEffect, useState } from 'react'
import type {
  BenchRunRowsResponse,
  LaunchParam,
  LlmRouteFull,
  ModelServerFull,
  ModelServersFullResponse,
  UtilServer,
  UtilizationResponse,
} from '@/lib/api/types'
import {
  Card,
  Code,
  EmptyState,
  KeyValue,
  Meter,
  Notice,
  Sparkline,
  StateChip,
  Text,
  type KeyValueItem,
} from '@/components/ui/primitives'
import { Button, ConfirmButton, Disclosure, Field, Input, Select, Toasts } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Cluster, ScrollX, Stack } from '@/components/layout'
import { useAction, useResource, type Resource } from '@/lib/swr'
import { K, modelServerAction, setLlmRoute } from '@/lib/api/endpoints'
import { formatDuration } from '@/lib/time'
import { gib } from '@/lib/format'
import { POOL_LABELS, SOURCE_LABELS, dotState, isGpu, isResident, serverState, useToasts } from './lib'

type ActKind = 'start' | 'stop' | 'sleep'
type Pending = { kind: ActKind; at: number; forced?: boolean }

/* -------------------------------------------------------------- copy chip */

function CopyButton({ value, label, onDone }: { value: string; label: string; onDone: (t: string) => void }) {
  return (
    <Button
      size="compact"
      className="coarse:min-h-control"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value)
          onDone(`Copied ${label}`)
        } catch {
          onDone(`Could not copy — ${value}`)
        }
      }}
    >
      copy
    </Button>
  )
}

/* ------------------------------------------------------------ fit verdict */

function FitPanel({ server, all }: { server: ModelServerFull; all: ModelServerFull[] }) {
  if (server.onbox === false || !isGpu(server)) return null
  const resident = isResident(server)
  const need = server.resident_gib_estimate ?? 0
  const pools = [server.memory_pool, ...(server.also_uses ?? [])].filter(
    (p): p is string => !!p && p !== 'host-ram' && p !== 'remote'
  )
  if (pools.length === 0) return null

  return (
    <Card title="Memory fit" hint={resident ? 'resident now' : 'projected onto its own pool'}>
      <Stack gap="sm">
        {pools.map((pool) => {
          const row = all.find((s) => s.memory_pool === pool && s.pool_total_gib != null)
          const total = row?.pool_total_gib ?? 0
          const used = row?.pool_used_gib ?? 0
          const add = resident ? 0 : need
          const over = total > 0 && used + add > total * 0.92
          const p = (v: number) => (total > 0 ? (v / total) * 100 : 0)
          return (
            <div key={pool}>
              <Meter
                label={`${POOL_LABELS[pool] ?? pool} projection`}
                segments={[
                  { key: 'used', pct: p(used), color: 'bg-live' },
                  ...(add > 0
                    ? [{ key: 'add', pct: p(Math.min(add, Math.max(0, total - used))), color: over ? 'bg-gone' : 'bg-accent' }]
                    : []),
                ]}
                left={
                  add > 0
                    ? `${used.toFixed(1)} resident + ${add.toFixed(1)} this = ${(used + add).toFixed(1)}`
                    : `${used.toFixed(1)} resident`
                }
                right={`${POOL_LABELS[pool] ?? pool} · ${total.toFixed(1)} GiB`}
              />
            </div>
          )
        })}
        {!resident &&
          (() => {
            const pool = server.memory_pool
            const row = all.find((s) => s.memory_pool === pool && s.pool_total_gib != null)
            const total = row?.pool_total_gib ?? 0
            const used = row?.pool_used_gib ?? 0
            const free = Math.max(0, total - used)
            if (total === 0) return null
            if (used + need <= total * 0.92)
              return (
                <Notice tone="ok">
                  <b>Fits.</b> {need.toFixed(1)} GiB needed, {free.toFixed(1)} GiB free on{' '}
                  {POOL_LABELS[pool ?? ''] ?? pool}.
                </Notice>
              )
            const samePool = all.filter((s) => isResident(s) && isGpu(s) && s.memory_pool === pool)
            return (
              <Notice tone="warn">
                <b>Will not fit.</b> Needs {need.toFixed(1)} GiB against {free.toFixed(1)} GiB free on{' '}
                {POOL_LABELS[pool ?? ''] ?? pool}.
                {samePool.length > 0 && (
                  <>
                    {' '}
                    Stop first (same pool only):
                    <ul className="mt-1.5 list-disc pl-4">
                      {samePool.map((r) => (
                        <li key={r.slug} className="wrap-anywhere font-mono text-body">
                          {r.slug} — frees {(r.resident_gib_estimate ?? 0).toFixed(1)} GiB
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </Notice>
            )
          })()}
      </Stack>
    </Card>
  )
}

/* -------------------------------------------------------------- telemetry */

function SlotsPanel({ u, cpu }: { u: UtilServer; cpu: boolean }) {
  const total = u.total_slots ?? 0
  const busy = u.busy_slots ?? 0
  const occupancyKnown = u.busy_slots != null && u.total_slots != null
  const drift =
    (u.declared_slots != null && u.total_slots != null && u.declared_slots !== u.total_slots) ||
    (u.declared_ctx_per_slot != null && u.ctx_per_slot != null && u.declared_ctx_per_slot !== u.ctx_per_slot)

  return (
    <Card title="Slots · live" hint="saturated is the alarm, not full">
      <Stack gap="sm">
        {occupancyKnown ? (
          <Meter
            label="slot occupancy"
            segments={[
              {
                key: 'busy',
                pct: total > 0 ? (busy / total) * 100 : 0,
                // Full is normal (one slot per consumer); QUEUING is not.
                color: u.saturated ? 'bg-gone' : cpu ? 'bg-ink-faint' : 'bg-live',
              },
            ]}
            left={
              u.metrics_available && u.predicted_tokens_per_second != null
                ? `${u.predicted_tokens_per_second.toFixed(1)} tok/s out · ${(u.prompt_tokens_per_second ?? 0).toFixed(0)} prefill`
                : 'throughput unavailable'
            }
            right={
              u.requests_deferred != null && u.requests_deferred > 0
                ? `${u.requests_deferred} queued`
                : `${busy} / ${total || '?'} busy`
            }
          />
        ) : (
          <Text>
            Occupancy unreadable on this runtime.
            {u.bench_decode_tok_s != null &&
              ` Benchmarked ${u.bench_decode_tok_s.toFixed(0)} tok/s decode · ${(u.bench_prefill_tok_s ?? 0).toFixed(0)} prefill (${u.benchmarked_at ?? ''}).`}
          </Text>
        )}

        {u.saturated && (
          <Notice tone="warn">
            <b>Requests are queuing.</b> A queued request takes whichever slot frees first, not the one
            holding its prefix — warm caches decay into cold prefills until this clears.
          </Notice>
        )}

        {drift && (
          <Notice tone="warn">
            <b>Unit and running server disagree.</b> Launch file declares {u.declared_slots ?? '?'} ×{' '}
            {u.declared_ctx_per_slot ?? '?'}; the server reports {u.total_slots ?? '?'} × {u.ctx_per_slot ?? '?'}
            — edited but never restarted.
          </Notice>
        )}

        {(u.telemetry_hint || (u.metrics_available === false && u.metrics_hint)) && (
          <Text clamp={4} className="text-micro">
            {u.telemetry_hint || u.metrics_hint}
          </Text>
        )}

        {u.prompt_cache_kind && (
          <p className="m-0 text-micro text-ink-faint">
            {u.prompt_cache_kind} cache
            {u.prompt_cache_used ? ` · ${u.prompt_cache_used} used` : ''}
            {u.prompt_cache_capacity ? ` of ${u.prompt_cache_capacity}` : ''}
          </p>
        )}
      </Stack>
    </Card>
  )
}

/* ------------------------------------------------------------ launch knobs */

function LaunchPanel({
  server,
  busyKind,
  onStart,
}: {
  server: ModelServerFull
  busyKind: ActKind | null
  onStart: (overrides: Record<string, string> | null) => void
}) {
  const params = server.parameters ?? []
  const [draft, setDraft] = useState<Record<string, string>>({})

  // Reset when the slug changes so a value typed against one model is never
  // submitted against another.
  useEffect(() => setDraft({}), [server.slug])

  if (server.onbox === false) return null
  const running = isResident(server)
  const effective = (p: LaunchParam) => draft[p.name] ?? p.value ?? ''
  const overrides = Object.fromEntries(
    params
      .filter((p) => effective(p) !== '' && (draft[p.name] !== undefined || p.source === 'aria_override'))
      .map((p) => [p.name, effective(p)])
  )

  return (
    <Card title="Launch configuration">
      {params.length === 0 ? (
        <EmptyState>
          Frozen in {server.deployment ? `${server.deployment}'s ` : ''}unit — no selectable launch
          parameters. Edit the deployment to change how it loads.
        </EmptyState>
      ) : (
        <Disclosure summary={<span className="text-body text-ink-dim">{params.length} parameters · how it loads</span>}>
          <Stack gap="sm">
            {params.map((p) => {
              const chosen = p.choices?.find((c) => c.value === effective(p))
              return (
                <Field
                  key={p.name}
                  label={`${p.label ?? p.name}${p.env ? ` · ${p.env}` : ''}`}
                  hint={
                    <>
                      <span className={p.source === 'aria_override' ? 'text-accent' : undefined}>
                        {SOURCE_LABELS[p.source ?? 'unset'] ?? p.source}
                      </span>
                      {chosen?.description ? ` — ${chosen.description}` : ''}
                      {p.description ? (
                        <span className="block wrap-anywhere">{p.description}</span>
                      ) : null}
                    </>
                  }
                >
                  {p.kind === 'enum' && p.choices?.length ? (
                    <Select
                      // Same coarse:text-title workaround as the fleet filter:
                      // the shared control's text-body beats the base 16px rule.
                      className="coarse:text-title"
                      value={effective(p)}
                      onChange={(e) => setDraft((d) => ({ ...d, [p.name]: e.target.value }))}
                    >
                      {p.choices.map((c) => (
                        <option key={c.value} value={c.value}>
                          {c.value}
                        </option>
                      ))}
                    </Select>
                  ) : (
                    <Input
                      className="coarse:text-title"
                      value={effective(p)}
                      inputMode={p.kind === 'int' ? 'numeric' : 'text'}
                      onChange={(e) => setDraft((d) => ({ ...d, [p.name]: e.target.value }))}
                    />
                  )}
                </Field>
              )
            })}

            <Cluster>
              <Button
                variant="primary"
                disabled={running || server.startable === false}
                busy={busyKind === 'start'}
                onClick={() => onStart(overrides)}
              >
                Start with these settings
              </Button>
              {/* A real, distinct action: clears ARIA's overrides AND starts,
                  so an experiment's context size cannot silently outlive it. */}
              <Button
                disabled={running || server.startable === false}
                busy={busyKind === 'start'}
                onClick={() => {
                  setDraft({})
                  onStart(null)
                }}
              >
                Start with defaults
              </Button>
            </Cluster>

            {running && (
              <Notice tone="info">
                Running with the configuration shown. These are load-time settings — stop and start again
                to apply a change.
              </Notice>
            )}
          </Stack>
        </Disclosure>
      )}
    </Card>
  )
}

/* -------------------------------------------------------------- benchmarks */

function BenchPanel({ server }: { server: ModelServerFull }) {
  const [open, setOpen] = useState(false)
  // Fetched only once opened: a 25-run scan of evalstack results has no
  // business on the critical path of a control surface.
  const runs = useResource<BenchRunRowsResponse>(open ? K.benchRuns(25) : null, { tier: 'lazy' })

  return (
    <Card title="Benchmarks" hint="matched by target slug/port">
      {!open ? (
        <Button onClick={() => setOpen(true)}>Load benchmark results</Button>
      ) : (
        <Async r={runs} skeletonRows={3}>
          {(d) => {
            const mine = (d.runs ?? []).filter((r) =>
              (r.targets ?? []).some(
                (t) => t.includes(server.slug) || (server.port != null && t.includes(String(server.port)))
              )
            )
            const byMetric = new Map<string, { label: string; value: number }[]>()
            for (const r of mine)
              for (const m of r.metrics ?? []) {
                const arr = byMetric.get(m.metric) ?? []
                arr.push({ label: m.benchmark, value: m.value })
                byMetric.set(m.metric, arr)
              }
            if (byMetric.size === 0)
              return (
                <EmptyState>
                  No benchmark results recorded for {server.slug}. Start it and run one — results land here.
                </EmptyState>
              )
            return (
              <Stack gap="sm">
                {[...byMetric.entries()].map(([metric, rows]) => (
                  <div key={metric} className="min-w-0">
                    <ScrollX>
                      <table className="w-full border-collapse text-body">
                        <thead>
                          <tr>
                            <th className="border-b border-line py-1.5 text-left text-micro font-medium uppercase tracking-[0.1em] text-ink-faint">
                              {metric}
                            </th>
                            <th className="border-b border-line py-1.5 text-right text-micro font-medium uppercase tracking-[0.1em] text-ink-faint">
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
              </Stack>
            )
          }}
        </Async>
      )}
    </Card>
  )
}

/* ------------------------------------------------------------------- body */

function Body({
  server,
  all,
  route,
  util,
}: {
  server: ModelServerFull
  all: ModelServerFull[]
  route: LlmRouteFull | undefined
  util: UtilServer | undefined
}) {
  const run = useAction()
  const { toasts, push, dismiss } = useToasts()
  const [busyKind, setBusyKind] = useState<ActKind | null>(null)
  const [routeBusy, setRouteBusy] = useState(false)
  const [pending, setPending] = useState<Pending | null>(null)
  // The action error slot: a successful poll never clears it (see header).
  const [actionError, setActionError] = useState<{ kind: ActKind; detail: string } | null>(null)

  const st = serverState(server)
  const running = isResident(server)
  const serving = route?.serving === server.slug
  const pinned = route?.pinned === server.slug

  // Retire the pending banner once the poll confirms the transition landed.
  useEffect(() => {
    if (!pending) return
    if (pending.kind === 'start' && st === 'running') {
      setPending(null)
      push('ok', `${server.slug} is up`)
    }
    if ((pending.kind === 'stop' || pending.kind === 'sleep') && !running) setPending(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [st, running])

  async function act(kind: ActKind, opts?: { force?: boolean; overrides?: Record<string, string> | null }) {
    setBusyKind(kind)
    const body =
      kind === 'start'
        ? opts?.overrides && Object.keys(opts.overrides).length
          ? { force: !!opts?.force, overrides: opts.overrides }
          : { force: !!opts?.force }
        : undefined
    const ok = await run(() => modelServerAction(server.slug, kind, body), {
      invalidate: ['/infrastructure/model-servers', '/infrastructure/llm-route'],
      onError: (e) =>
        setActionError({
          kind,
          detail: typeof e.detail === 'string' ? e.detail : e.message,
        }),
    })
    setBusyKind(null)
    if (ok !== undefined) {
      setActionError(null)
      setPending({ kind, at: Date.now(), forced: opts?.force })
      push('ok', `${kind} requested`)
    }
  }

  async function pin(slug: string | null) {
    setRouteBusy(true)
    const ok = await run(() => setLlmRoute(slug), {
      invalidate: ['/infrastructure/llm-route'],
      onError: (e) => setActionError({ kind: 'start', detail: `route: ${e.message}` }),
    })
    setRouteBusy(false)
    if (ok !== undefined) push('ok', slug ? `Pinned ${slug}` : 'Unpinned — route is auto')
  }

  // Stop is two-tap when something depends on this server being up.
  const stopIsRisky = serving || pinned || (server.bound_agents?.length ?? 0) > 0 || !!server.consumers_note

  const endpoint = server.endpoints?.tailnet || server.endpoints?.local

  const identItems: KeyValueItem[] = [
    { k: 'Resident', v: isGpu(server) ? gib(server.resident_gib_measured ?? server.resident_gib_estimate) : 'CPU only', kind: 'num' },
    {
      k: 'Memory pool',
      v: [
        POOL_LABELS[server.memory_pool ?? ''] ?? server.memory_pool ?? '—',
        ...(server.also_uses ?? []).map((p) => `+ ${POOL_LABELS[p] ?? p}`),
      ].join(' '),
      kind: 'prose',
    },
    {
      k: 'Serves',
      v: server.served_ctx ? `${server.served_ctx.toLocaleString()} ctx × ${server.slots ?? 1}` : '—',
      kind: 'num',
    },
    { k: 'Port', v: server.port ?? '—', kind: 'num' },
    { k: 'Device', v: server.devices?.join(' + ') || server.backend_device || '—', kind: 'ident' },
    { k: 'Runtime', v: [server.runtime_repo, server.runtime_ref].filter(Boolean).join(' — ') || '—', kind: 'ident' },
    { k: 'Weights', v: server.model_file ?? '—', kind: 'ident' },
    ...(endpoint
      ? [
          {
            k: 'Endpoint',
            v: (
              <span className="flex flex-wrap items-center gap-2">
                <Code>{endpoint}</Code>
                <CopyButton value={endpoint} label="endpoint" onDone={(t) => push('ok', t)} />
              </span>
            ),
          } satisfies KeyValueItem,
        ]
      : []),
    ...(server.systemd_unit || server.launch_script
      ? [{ k: 'Unit', v: server.systemd_unit ?? server.launch_script ?? '—', kind: 'ident' as const }]
      : []),
    ...(server.geometry_source ? [{ k: 'Geometry source', v: server.geometry_source, kind: 'prose' as const }] : []),
    ...(server.consumers_note ? [{ k: 'Consumers', v: server.consumers_note, kind: 'prose' as const }] : []),
  ]

  const description = server.description ?? ''
  const longDescription = description.length > 220

  return (
    <Stack>
      <Card
        title="Model"
        actions={
          <>
            <Button
              variant="primary"
              disabled={running || server.startable === false || server.onbox === false}
              busy={busyKind === 'start'}
              onClick={() => act('start')}
            >
              {running ? 'Running' : 'Start'}
            </Button>
            {stopIsRisky ? (
              <ConfirmButton
                label="Stop"
                confirmLabel={serving || pinned ? 'Stop the serving model?' : 'Stop — consumers exist?'}
                disabled={!running || server.onbox === false}
                onConfirm={() => act('stop')}
              />
            ) : (
              <Button disabled={!running || server.onbox === false} busy={busyKind === 'stop'} onClick={() => act('stop')}>
                Stop
              </Button>
            )}
            {server.onbox === false && server.can_sleep !== false && (
              <ConfirmButton label="Sleep host" confirmLabel="Sleep the remote host?" onConfirm={() => act('sleep')} />
            )}
            {pinned ? (
              <Button busy={routeBusy} onClick={() => pin(null)}>
                Unpin
              </Button>
            ) : (
              <Button
                disabled={!running || server.onbox === false}
                busy={routeBusy}
                onClick={() => pin(server.slug)}
                title="Make this the local model ARIA and Hermes talk to"
              >
                Serve this
              </Button>
            )}
          </>
        }
      >
        <Stack gap="sm">
          <Cluster>
            <h3 className="m-0 min-w-0 wrap-anywhere font-mono text-title font-semibold">{server.slug}</h3>
            <StateChip state={dotState(server)} note={st === 'ready' ? 'unit not created yet' : undefined} />
            {serving && (
              <span className="whitespace-nowrap rounded-sm border border-accent px-1.5 py-0.5 text-micro uppercase tracking-[0.08em] text-accent">
                serving {pinned ? '· pinned' : '· auto'}
              </span>
            )}
          </Cluster>

          {description &&
            (longDescription ? (
              <Disclosure summary={<Text clamp={3}>{description}</Text>}>
                <Text>{description}</Text>
              </Disclosure>
            ) : (
              <Text clamp={3}>{description}</Text>
            ))}

          {/* -------- action feedback: pending, then the truth from the poll */}
          {actionError && (
            <Notice tone="warn">
              <Stack gap="sm">
                <span className="wrap-anywhere">
                  <b>{actionError.kind} refused.</b> {actionError.detail}
                </span>
                <Cluster>
                  {actionError.kind === 'start' && server.startable !== false && (
                    <ConfirmButton
                      label="Force start"
                      confirmLabel="Override the registry guards?"
                      onConfirm={() => act('start', { force: true })}
                    />
                  )}
                  <Button onClick={() => setActionError(null)}>Dismiss</Button>
                </Cluster>
              </Stack>
            </Notice>
          )}

          {(pending?.kind === 'start' || st === 'loading') && !(st === 'running' || st === 'ready') && (
            <Notice tone="info">
              <b>Loading…</b>
              {pending ? ` ${formatDuration((Date.now() - pending.at) / 1000)} elapsed.` : ''} Large checkpoints
              stream from disk — Radiance takes ~7 minutes cold. This panel updates as the registry reports
              progress; leaving the page is safe.
            </Notice>
          )}
          {pending && pending.kind !== 'start' && running && (
            <Notice tone="info">
              <b>{pending.kind} requested</b> · {formatDuration((Date.now() - pending.at) / 1000)} ago — waiting
              for the registry to confirm.
            </Notice>
          )}

          {server.startable === false && server.not_startable_reason && (
            <Notice tone="warn">
              <span className="wrap-anywhere">
                <b>Retired.</b> {server.not_startable_reason}
              </span>
            </Notice>
          )}

          <KeyValue layout="stack" items={identItems} />
        </Stack>
      </Card>

      <FitPanel server={server} all={all} />
      {util && <SlotsPanel u={util} cpu={!isGpu(server)} />}
      <LaunchPanel server={server} busyKind={busyKind} onStart={(o) => act('start', { overrides: o })} />
      <BenchPanel server={server} />

      <p className="m-0 text-micro text-ink-faint">
        Fleet state polls every 30s; slot telemetry every 10s.{' '}
        <Link href="/operate" className="text-accent underline underline-offset-2" data-inline>
          Back to the fleet
        </Link>
      </p>
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </Stack>
  )
}

/* ------------------------------------------------------------------ shell */

export function ServerDetail({ slug }: { slug: string }) {
  // The detail needs the FULL row (description, launch parameters, exclusivity,
  // consumer notes) — which the list view deliberately omits, since re-sending
  // 65 KB of static registry prose on every fleet poll is most of what made
  // this screen heavy. One server, fetched by slug.
  const detail = useResource<ModelServerFull>(K.modelServer(slug), { tier: 'slow' })
  // The fit panel needs the rest of the fleet (same-pool residents, pool
  // totals) — all of which the list view carries. Same key as the layout and
  // spine, so SWR serves it from cache and the detail costs no extra request.
  const fleet = useResource<ModelServersFullResponse>(K.modelServers, { tier: 'slow' })
  const route = useResource<LlmRouteFull>(K.llmRoute, { tier: 'slow' })
  const utilization = useResource<UtilizationResponse>(K.utilization, { tier: 'fast' })

  return (
    <Async r={detail} skeletonRows={8}>
      {(server) => {
        if (!server || !server.slug)
          return (
            <Notice tone="warn">
              No registry entry named <Code>{slug}</Code>.{' '}
              <Link href="/operate" className="text-accent underline underline-offset-2">
                Back to the fleet
              </Link>
            </Notice>
          )
        return (
          <Body
            server={server}
            all={fleet.data?.servers ?? []}
            route={route.data}
            util={utilization.data?.servers?.find((u) => u.slug === slug && u.reachable)}
          />
        )
      }}
    </Async>
  )
}
