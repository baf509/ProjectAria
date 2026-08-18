'use client'

/**
 * ARIA - Operate: master/detail shell
 *
 * The fleet list lives HERE so selection is routing, not useState — the old
 * page's `useState(sel)` meant tapping a fleet row on a phone appeared to do
 * nothing (the detail rendered 1.2kpx below) and Back did nothing. The list
 * persists across detail navigations, so its scroll position and filter
 * survive; below lg it hides when a detail segment is open and the TopBar
 * gets a real Back chip.
 *
 * Route group `(fleet)` scopes this layout to the spine/servers/services
 * screens without touching `/operate/benchmarks`, which is its own page.
 *
 * DOM order vs display order: on the phone the spine (children) must come
 * first and the fleet second; at lg the fleet is the left column. `order-*`
 * flips it rather than duplicating the markup.
 */
import { ReactNode } from 'react'
import { useSelectedLayoutSegment } from 'next/navigation'
import { AppShell, StatusStat } from '@/components/shell/AppShell'
import { useResource } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type {
  LlmRouteFull,
  ModelServersFullResponse,
  ServicesResponse,
  UtilizationResponse,
} from '@/lib/api/types'
import { FleetList } from '@/features/operate/FleetList'
import { derivePools, isResident } from '@/features/operate/lib'

export default function OperateLayout({ children }: { children: ReactNode }) {
  // `slow` for the fleet is a hard rule: the payload is 73KB and the endpoint
  // measured 8.8s server-side — fast-polling it is what made the old
  // dashboard feel broken. Only slot telemetry earns the fast tier.
  const fleet = useResource<ModelServersFullResponse>(K.modelServers, { tier: 'slow' })
  const services = useResource<ServicesResponse>(K.services, { tier: 'slow' })
  const route = useResource<LlmRouteFull>(K.llmRoute, { tier: 'slow' })
  const utilization = useResource<UtilizationResponse>(K.utilization, { tier: 'fast' })

  const segment = useSelectedLayoutSegment()
  const detailOpen = segment !== null

  const servers = fleet.data?.servers ?? []
  const pools = derivePools(servers)
  const onbox = servers.filter((s) => s.onbox !== false)
  const resident = onbox.filter(isResident).length
  const down = (services.data?.services ?? []).filter((s) => !s.healthy).length
  const serving = route.data?.serving
  const servingUtil = utilization.data?.servers?.find((u) => u.slug === serving && u.reachable)

  return (
    <AppShell
      area="Operate"
      back={detailOpen ? { href: '/operate', label: 'Fleet' } : undefined}
      status={
        <>
          {/* Alarms first: the strip scrolls on the phone, so the leftmost
              slot is the only guaranteed-visible one. */}
          <StatusStat label="SERVICES" tone={down > 0 ? 'warn' : 'ok'}>
            {services.data ? (down > 0 ? `${down} down` : 'ok') : '…'}
          </StatusStat>
          {pools.map((p) => (
            <StatusStat key={p.pool} tone={p.spilling ? 'warn' : 'default'} label={p.label.toUpperCase()}>
              {p.used_gib.toFixed(0)}/{p.total_gib.toFixed(0)} GiB
            </StatusStat>
          ))}
          <StatusStat label="RESIDENT">
            {fleet.data ? `${resident} of ${onbox.length}` : '…'}
          </StatusStat>
          <StatusStat label="SERVING" tone={servingUtil?.saturated ? 'warn' : 'default'}>
            {serving ?? '—'}
            {route.data?.pinned ? ' · pinned' : ''}
            {servingUtil?.saturated ? ' · queuing' : ''}
          </StatusStat>
        </>
      }
    >
      <div className="flex min-w-0 flex-col gap-gap lg:grid lg:grid-cols-[minmax(16rem,0.8fr)_minmax(0,1.7fr)] lg:items-start">
        <div className={`order-2 min-w-0 lg:order-1 ${detailOpen ? 'hidden lg:block' : ''}`}>
          <FleetList fleet={fleet} />
        </div>
        <div className="order-1 min-w-0 lg:order-2">{children}</div>
      </div>
    </AppShell>
  )
}
