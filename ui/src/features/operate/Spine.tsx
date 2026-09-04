'use client'

/**
 * ARIA - Operate: the phone spine (/operate index)
 *
 * The first screen of Operate, absorbing the old `/` overview. Order is
 * alarms → memory → route → residents → services, i.e. worst news first: the
 * old page announced downed services in the status bar and made them fixable
 * only four viewports down.
 *
 * Memory is drawn by MemoryPools, which renders the machine's actual topology:
 * ONE system-memory bar (the Strix Halo iGPU has no memory of its own — its GTT
 * allocation is system RAM, so `halo-gtt` and `host-ram` are the same DIMMs and
 * host-ram's figure already contains the iGPU's) plus a separate bar for the
 * R9700's own VRAM, which the old overview omitted entirely.
 */
import Link from 'next/link'
import { useState } from 'react'
import type {
  DevicesResponse,
  LlmRouteFull,
  ModelServersFullResponse,
  ServiceFull,
  ServicesResponse,
  UtilizationResponse,
} from '@/lib/api/types'
import { Card, EmptyState, Notice, StatusDot, Text } from '@/components/ui/primitives'
import { MemoryPools } from './MemoryPools'
import { Button, Toasts } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Cluster, Row, Stack } from '@/components/layout'
import { useAction, type Resource } from '@/lib/swr'
import { modelServerAction, serviceAction, setLlmRoute } from '@/lib/api/endpoints'
import { api, ApiError } from '@/lib/http'
import { gib, middleTruncate, pct } from '@/lib/format'
import { STATE_WORD, dotState, isResident, serverState, sortServices, useToasts, utilFor } from './lib'

const RADIANCE = 'Qwen3.8-27B-R9700-Radiance'
const FLASH_HALO = 'Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K'
const FLASH_HYBRID = 'Qwen3.8-Flash-Next-Hybrid-R9700-Halo'

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

async function waitForModel(slug: string, running: boolean, timeoutMs = 20 * 60_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const server = await api<{ state?: string }>(`/infrastructure/model-servers/${encodeURIComponent(slug)}`)
    if ((server.state === 'running') === running) return
    if (server.state === 'dead') throw new Error(`${slug} failed while loading`)
    await delay(5_000)
  }
  throw new Error(`${slug} did not become ${running ? 'ready' : 'stopped'} within 20 minutes`)
}

/* ------------------------------------------------------------------ alarms */

function ServiceAlarm({
  service,
  onDone,
  onError,
}: {
  service: ServiceFull
  onDone: (t: string) => void
  onError: (t: string) => void
}) {
  const run = useAction()
  const [busy, setBusy] = useState(false)
  return (
    <Notice tone="warn">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1">
          <b>{service.slug}</b> is {service.state ?? 'down'} — expected always up.
        </span>
        {service.manageable !== false && (
          <Button
            variant="primary"
            busy={busy}
            onClick={async () => {
              setBusy(true)
              const ok = await run(() => serviceAction(service.slug, 'start'), {
                invalidate: ['/infrastructure/services'],
                onError: (e) => onError(`${service.slug}: ${e.message}`),
              })
              setBusy(false)
              if (ok !== undefined) onDone(`${service.slug} start requested`)
            }}
          >
            Start
          </Button>
        )}
      </div>
    </Notice>
  )
}

/* ------------------------------------------------------------------- spine */

export function Spine({
  fleet,
  services,
  route,
  utilization,
  devices,
}: {
  fleet: Resource<ModelServersFullResponse>
  services: Resource<ServicesResponse>
  route: Resource<LlmRouteFull>
  utilization: Resource<UtilizationResponse>
  devices: Resource<DevicesResponse>
}) {
  const { toasts, push, dismiss } = useToasts()
  const run = useAction()
  const [routeBusy, setRouteBusy] = useState<string | null>(null)
  const [loadoutBusy, setLoadoutBusy] = useState<'dual' | 'hybrid' | null>(null)
  const [loadoutProgress, setLoadoutProgress] = useState<string | null>(null)
  // Action errors live apart from poll errors: a successful background poll
  // must not wipe the reason a start was refused off the screen.
  const [actionError, setActionError] = useState<string | null>(null)

  const unhealthy = (services.data?.services ?? []).filter((s) => !s.healthy)
  const saturated = (utilization.data?.servers ?? []).filter((u) => u.reachable && u.saturated)

  async function pin(slug: string | null) {
    setRouteBusy(slug ?? 'auto')
    const ok = await run(() => setLlmRoute(slug), {
      invalidate: ['/infrastructure/llm-route'],
      onError: (e) => setActionError(`route: ${e.message}`),
    })
    setRouteBusy(null)
    if (ok !== undefined) {
      setActionError(null)
      push('ok', slug ? `Pinned ${slug}` : 'Route set to auto')
    }
  }

  async function activateLoadout(loadout: 'dual' | 'hybrid') {
    setLoadoutBusy(loadout)
    setActionError(null)
    try {
      if (loadout === 'dual') {
        setLoadoutProgress('Stopping the hybrid server…')
        await modelServerAction(FLASH_HYBRID, 'stop')
        await waitForModel(FLASH_HYBRID, false)

        setLoadoutProgress('Loading Qwen3.8 on the R9700…')
        await modelServerAction(RADIANCE, 'start')
        await waitForModel(RADIANCE, true)

        setLoadoutProgress('Radiance is ready; loading Flash Next on the Halo…')
        await modelServerAction(FLASH_HALO, 'start')
        await waitForModel(FLASH_HALO, true)
      } else {
        setLoadoutProgress('Unloading the Halo-only server…')
        await modelServerAction(FLASH_HALO, 'stop')
        await waitForModel(FLASH_HALO, false)

        setLoadoutProgress('Unloading Radiance from the R9700…')
        await modelServerAction(RADIANCE, 'stop')
        await waitForModel(RADIANCE, false)

        setLoadoutProgress('Loading Flash Next across the R9700 and Halo…')
        await modelServerAction(FLASH_HYBRID, 'start')
        await waitForModel(FLASH_HYBRID, true)
      }

      // A remembered pin to the previous loadout would force every request
      // through fallback selection. Auto follows the newly selected residents.
      await setLlmRoute(null)
      await Promise.all([fleet.refresh(), route.refresh(), devices.refresh(), utilization.refresh()])
      push('ok', loadout === 'dual' ? 'Dual-resident Qwen loadout is ready' : 'Hybrid Flash Next is ready')
      setLoadoutProgress(null)
    } catch (err) {
      const message = err instanceof ApiError || err instanceof Error ? err.message : String(err)
      setActionError(`loadout: ${message}`)
      setLoadoutProgress(null)
    } finally {
      setLoadoutBusy(null)
    }
  }

  return (
    <Stack>
      {actionError && (
        <Notice tone="warn">
          <div className="flex flex-wrap items-center gap-2">
            <span className="min-w-0 flex-1 wrap-anywhere">{actionError}</span>
            <Button onClick={() => setActionError(null)}>Dismiss</Button>
          </div>
        </Notice>
      )}

      {unhealthy.map((s) => (
        <ServiceAlarm key={s.slug} service={s} onDone={(t) => push('ok', t)} onError={(t) => setActionError(t)} />
      ))}

      {saturated.map((u) => (
        <Notice key={u.slug} tone="warn">
          <b>{u.slug} is queuing.</b> {u.requests_deferred ?? '?'} deferred — a queued request lands in
          whichever slot frees first, not the one holding its prefix, so expect cold prefills until this
          clears.
        </Notice>
      ))}

      <MemoryPools
        devices={devices}
        residents={(fleet.data?.servers ?? []).filter((srv) => isResident(srv))}
      />

      <Card title="Model loadout" hint="one safe switch for both GPUs">
        <Stack gap="sm">
          <Cluster>
            <Button
              variant="primary"
              busy={loadoutBusy === 'dual'}
              disabled={loadoutBusy !== null}
              aria-pressed={
                (fleet.data?.servers ?? []).some((s) => s.slug === RADIANCE && isResident(s)) &&
                (fleet.data?.servers ?? []).some((s) => s.slug === FLASH_HALO && isResident(s))
              }
              onClick={() => activateLoadout('dual')}
            >
              Load Qwen dual resident
            </Button>
            <Button
              variant="primary"
              busy={loadoutBusy === 'hybrid'}
              disabled={loadoutBusy !== null}
              aria-pressed={(fleet.data?.servers ?? []).some((s) => s.slug === FLASH_HYBRID && isResident(s))}
              onClick={() => activateLoadout('hybrid')}
            >
              Load Flash Next hybrid
            </Button>
          </Cluster>
          <Text>
            Dual resident starts Radiance on the R9700, waits for readiness, then starts Flash Next on the Halo.
            Hybrid unloads both and runs one tuned Flash Next process across both GPUs.
          </Text>
          {loadoutProgress && <Notice tone="info">{loadoutProgress}</Notice>}
        </Stack>
      </Card>

      <Card title="Local model route" hint="ARIA + Hermes follow this">
        <Async r={route} skeletonRows={2}>
          {(r) => {
            const loaded = r.loaded ?? []
            if (loaded.length === 0)
              return <EmptyState>Nothing is loaded — start a model and both ARIA and Hermes follow it.</EmptyState>
            return (
              <Stack gap="sm">
                <Cluster>
                  <Button
                    aria-pressed={!r.pinned}
                    busy={routeBusy === 'auto'}
                    disabled={routeBusy !== null}
                    className={!r.pinned ? 'border-accent text-accent' : undefined}
                    onClick={() => pin(null)}
                  >
                    Auto
                  </Button>
                  {loaded.map((m) => (
                    <Button
                      key={m.slug}
                      aria-pressed={r.pinned === m.slug}
                      busy={routeBusy === m.slug}
                      disabled={routeBusy !== null}
                      className={r.pinned === m.slug ? 'border-accent text-accent' : undefined}
                      onClick={() => pin(m.slug)}
                      title={m.slug}
                    >
                      {middleTruncate(m.slug, 26)}
                    </Button>
                  ))}
                </Cluster>
                <Text>
                  Serving <span className="font-mono text-ink">{r.serving ?? '—'}</span>
                  {r.reason ? ` — ${r.reason}` : ''}
                </Text>
                {loaded.length > 1 && !r.pinned && (
                  <Notice tone="info">
                    {loaded.length} models are loaded; auto picks the largest. Pin one to make the choice
                    explicit.
                  </Notice>
                )}
              </Stack>
            )
          }}
        </Async>
      </Card>

      <Card title="Resident now" bodyClassName="p-0">
        <Async
          r={fleet}
          skeletonRows={3}
          isEmpty={(d) => d.servers.filter(isResident).length === 0}
          empty="Nothing is resident. Pick a model from the fleet below."
        >
          {(d) => (
            <ul className="m-0 list-none p-0">
              {d.servers.filter(isResident).map((s) => {
                const st = serverState(s)
                const u = utilFor(utilization.data?.servers, s.slug)
                const slots =
                  u && u.busy_slots != null && u.total_slots != null ? `${u.busy_slots}/${u.total_slots} busy` : null
                return (
                  <li key={s.slug} className="border-b border-line last:border-b-0">
                    <Row
                      as={Link}
                      href={`/operate/servers/${encodeURIComponent(s.slug)}`}
                      marker={<StatusDot state={dotState(s)} />}
                      trailing={s.resident_gib_estimate ? gib(s.resident_gib_estimate) : 'cpu'}
                      className="px-2.5 py-1.5 hover:bg-panel-2"
                    >
                      <span className="block wrap-anywhere font-mono text-label text-ink">{s.slug}</span>
                      <span className={`text-micro ${STATE_WORD[st].tone}`}>
                        {STATE_WORD[st].word}
                        {slots ? ` · ${slots}` : ''}
                        {u?.saturated ? ' · QUEUING' : ''}
                        {u?.slot_utilisation != null ? ` · ${pct(u.slot_utilisation)}` : ''}
                      </span>
                    </Row>
                  </li>
                )
              })}
            </ul>
          )}
        </Async>
      </Card>

      <Card title="Services" hint="stopped on_demand is normal; a downed always_up pages" bodyClassName="p-0">
        <Async r={services} skeletonRows={5}>
          {(d) => (
            <ul className="m-0 list-none p-0">
              {sortServices(d.services).map((s) => {
                const stopped = s.state !== 'running'
                return (
                  <li key={s.slug} className="border-b border-line last:border-b-0">
                    <Row
                      as={Link}
                      href={`/operate/services/${encodeURIComponent(s.slug)}`}
                      marker={<StatusDot state={s.healthy ? (stopped ? 'external' : 'running') : 'absent'} />}
                      trailing={s.port ? `:${s.port}` : ''}
                      className="px-2.5 py-1.5 hover:bg-panel-2"
                    >
                      <span className="block wrap-anywhere font-mono text-label text-ink">
                        {s.slug}
                        {s.needs_review && (
                          <span className="ml-1.5 text-micro text-idle" title="expected_state inferred, not confirmed">
                            ⚠
                          </span>
                        )}
                      </span>
                      <span className={`text-micro ${s.healthy ? 'text-ink-faint' : 'text-gone'}`}>
                        {s.state}
                        {s.expected_state === 'on_demand' ? ' · on demand' : ''}
                        {!s.healthy ? ' · expected up' : ''}
                      </span>
                    </Row>
                  </li>
                )
              })}
            </ul>
          )}
        </Async>
      </Card>

      {(fleet.stale || services.stale) && (
        <Notice tone="warn">Showing the last known state — the API is not responding.</Notice>
      )}
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </Stack>
  )
}
