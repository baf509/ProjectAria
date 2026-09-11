'use client'

/**
 * ARIA - Operate: the phone spine (/operate index)
 *
 * Organised by MACHINE. The previous version was organised by kind — one
 * Memory card, one Temperatures card, one "Resident now" card, one Corsair
 * loadout card — so answering "what is Red doing?" meant reading four cards and
 * joining them by hand, and the Corsair loadout button stood alone with no
 * statement of what was already resident on that box (it read "Load and select
 * Flash Next" while Flash Next was loaded).
 *
 * Order is alarms → machines → default route → unclaimed telemetry. Worst news
 * still first: the old page announced downed services in the status bar and
 * made them fixable only four viewports down.
 *
 * Each machine card carries its own residency, memory and temperature, plus
 * whatever that box can be told to do. The Mac carries the service list,
 * because the services ARE what the control plane runs. The full model
 * catalogue stays in the fleet list (the layout's left column, and below the
 * spine on a phone).
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
import { Temperatures } from './Temperatures'
import { Diagnosis } from './Diagnosis'
import { RedModelControls } from './RedModels'
import { MachineCard } from './MachineCard'
import { buildMachines, unclaimedTemperatureHosts, type Machine } from './machines'
import { Button, Toasts } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Cluster, Row, Stack } from '@/components/layout'
import { useAction, type Resource } from '@/lib/swr'
import { modelServerAction, serviceAction, setLlmRoute } from '@/lib/api/endpoints'
import { api, ApiError } from '@/lib/http'
import { middleTruncate, pct } from '@/lib/format'
import { isResident, sortServices, useToasts } from './lib'

const FLASH_HYBRID = 'Qwen3.8-Flash-Next-CUDA-Halo-Candidate'

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

/* ------------------------------------------------------------------ services */

function ServiceList({ services }: { services: Resource<ServicesResponse> }) {
  return (
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
                  className="px-0 py-1.5 hover:bg-panel-2"
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
  const [loadoutBusy, setLoadoutBusy] = useState(false)
  const [loadoutProgress, setLoadoutProgress] = useState<string | null>(null)
  // Action errors live apart from poll errors: a successful background poll
  // must not wipe the reason a start was refused off the screen.
  const [actionError, setActionError] = useState<string | null>(null)

  const unhealthy = (services.data?.services ?? []).filter((s) => !s.healthy)
  const saturated = (utilization.data?.servers ?? []).filter((u) => u.reachable && u.saturated)
  const flash = fleet.data?.servers.find((s) => s.slug === FLASH_HYBRID)

  const machines = buildMachines(
    fleet.data?.servers ?? [],
    devices.data,
    utilization.data?.servers,
    route.data
  )
  const machine = (id: string) => machines.find((m) => m.spec.id === id) as Machine | undefined
  const strays = unclaimedTemperatureHosts(devices.data)

  // The Corsair loadout button used to be an unconditional "Load and select
  // Flash Next" — it said "load" while the model was resident, which is the
  // single most misleading thing on this page. Both facts are now in the label.
  //
  // Until BOTH resources answer, the button states neither. Defaulting to
  // "Load and select" while the fleet is still in flight is the same lie in a
  // smaller window: an unloaded answer asserted from an absent one. (Caught by
  // the phone gate, where a slow first fetch held that label for 10s.)
  const flashKnown = Boolean(fleet.data && route.data)
  const flashResident = Boolean(flash && isResident(flash))
  const flashPinned = route.data?.pinned === FLASH_HYBRID
  const flashLabel = !flashKnown
    ? 'Checking Flash Next…'
    : !flashResident
      ? 'Load and select Flash Next'
      : !flashPinned
        ? 'Select Flash Next'
        : 'Flash Next loaded and selected'

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

  async function activateLoadout() {
    // The API remains authoritative. Missing or engineering-only metadata
    // must never turn this convenience button into a qualification bypass.
    if (flash?.startable !== true) return
    setLoadoutBusy(true)
    setActionError(null)
    try {
      if (!isResident(flash)) {
        setLoadoutProgress('Loading Flash Next on RTX 3090 + Strix Halo…')
        // No forced eviction or unrelated Red/auxiliary stop. A conflicting
        // residency is refused by the normal model actuator for review.
        await modelServerAction(FLASH_HYBRID, 'start')
        await waitForModel(FLASH_HYBRID, true)
      }
      await setLlmRoute(FLASH_HYBRID)
      await Promise.all([fleet.refresh(), route.refresh(), devices.refresh(), utilization.refresh()])
      push('ok', 'RTX 3090 + Halo Flash Next is ready and selected')
      setLoadoutProgress(null)
    } catch (err) {
      const message = err instanceof ApiError || err instanceof Error ? err.message : String(err)
      setActionError(`loadout: ${message}`)
      setLoadoutProgress(null)
    } finally {
      setLoadoutBusy(false)
    }
  }

  const corsair = machine('corsair')
  const red = machine('red')
  const ridge = machine('ridge')
  const mac = machine('mac')

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

      <Diagnosis
        // A verdict is only as good as the resources behind it; until each has
        // answered, the card says so rather than grading an empty fleet.
        ready={![fleet, services, utilization, devices].some((r) => r.isLoading)}
        machines={machines}
        services={services.data?.services}
        util={utilization.data?.servers}
        route={route.data}
        servers={fleet.data?.servers}
      />

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

      {corsair && (
        <MachineCard machine={corsair}>
          <Stack gap="sm">
            <Cluster>
              <Button
                variant={!flashKnown || (flashResident && flashPinned) ? 'default' : 'primary'}
                busy={loadoutBusy}
                disabled={
                  loadoutBusy || !flashKnown || flash?.startable !== true ||
                  (flashResident && flashPinned)
                }
                aria-pressed={flashResident && flashPinned}
                onClick={activateLoadout}
              >
                {flashLabel}
              </Button>
            </Cluster>
            <Text>
              {!flashKnown
                ? 'Qwen Flash Next is the standing Corsair model, with one 256K context slot. Reading its current state…'
                : flashResident
                  ? 'Qwen Flash Next is resident with one 256K context slot. Selecting it makes it the default for requests that name no model.'
                  : 'Qwen Flash Next is the standing Corsair model, with one 256K context slot. This loads it and makes it the default for requests that name no model.'}
            </Text>
            {flashKnown && flash?.startable !== true && (
              <Notice tone="info">
                {flash?.not_startable_reason ?? 'Waiting for a qualified, registered Flash Next deployment.'}
              </Notice>
            )}
            {loadoutProgress && <Notice tone="info">{loadoutProgress}</Notice>}
          </Stack>
        </MachineCard>
      )}

      {red && (
        <MachineCard machine={red}>
          <RedModelControls fleet={fleet} route={route} utilization={utilization} />
        </MachineCard>
      )}

      {ridge && <MachineCard machine={ridge} />}

      {mac && (
        <MachineCard machine={mac}>
          <div className="min-w-0">
            <h3 className="m-0 mb-1.5 text-micro font-medium uppercase tracking-[0.14em] text-ink-faint">
              Services
            </h3>
            <Text>Stopped on_demand is normal; a downed always_up pages.</Text>
            <ServiceList services={services} />
          </div>
        </MachineCard>
      )}

      <Card title="Default route" hint="Answers requests that name no model">
        <Async r={route} skeletonRows={2}>
          {(r) => {
            const loaded = r.loaded ?? []
            if (loaded.length === 0)
              return <EmptyState>Nothing is loaded — start a model to serve requests without an explicit model.</EmptyState>
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

      {strays.length > 0 && <Temperatures devices={devices} hosts={strays} />}

      {(fleet.stale || services.stale) && (
        <Notice tone="warn">Showing the last known state — the API is not responding.</Notice>
      )}
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </Stack>
  )
}
