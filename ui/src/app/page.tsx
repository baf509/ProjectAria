/**
 * Overview — the landing surface.
 *
 * Was a marketing-styled launcher: gradients, serif display type, wide tracking,
 * a different design language from every operational page behind it. With the
 * shell carrying persistent nav, a launcher has no job left, so this is now an
 * actual at-a-glance page: is the API up, what is resident, what is costing
 * memory, and where the areas are.
 */
'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { AppShell, StatusStat } from '@/components/AppShell'
import { Card, EmptyState, Meter, StatusDot, normalizeState } from '@/components/ui'
import { apiClient } from '@/lib/api-client'

type Server = {
  slug: string
  state?: string
  resident_gib_estimate?: number | null
  backend_device?: string | null
  onbox?: boolean
  gtt_used_gib?: number
  gtt_total_gib?: number
}

const AREAS = [
  { href: '/chat', label: 'Converse', blurb: 'Chat, voice and conversation history.' },
  { href: '/cockpit', label: 'Supervise', blurb: 'Coding sessions, shells and agent work.' },
  { href: '/operate', label: 'Operate', blurb: 'Model servers, benchmarks and memory.' },
  { href: '/dashboard', label: 'Know', blurb: 'Memory, research, projects and usage.' },
]

export default function Overview() {
  const [api, setApi] = useState<'checking' | 'up' | 'down'>('checking')
  const [servers, setServers] = useState<Server[]>([])

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const h = await apiClient.checkHealth()
        if (!alive) return
        setApi(h.status === 'healthy' || h.status === 'degraded' ? 'up' : 'down')
        const d = await apiClient.listModelServers()
        if (alive) setServers(d?.servers ?? [])
      } catch {
        if (alive) setApi('down')
      }
    }
    tick()
    const t = setInterval(tick, 15000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  const onbox = servers.filter((s) => s.onbox !== false)
  const running = onbox.filter((s) => normalizeState(s.state) === 'running')
  const withGtt = servers.find((s) => s.gtt_total_gib)
  const total = withGtt?.gtt_total_gib ?? 124
  const used = withGtt?.gtt_used_gib ?? 0

  return (
    <AppShell
      area="Overview"
      status={
        <>
          <StatusStat label="API">
            {api === 'checking' ? '…' : api === 'up' ? 'connected' : 'unreachable'}
          </StatusStat>
          <StatusStat label="RESIDENT">
            {running.length} of {onbox.length}
          </StatusStat>
        </>
      }
    >
      {api === 'down' && (
        <div className="mb-3.5 rounded border border-gone bg-gone/10 px-3.5 py-2.5 font-sans text-xs">
          Cannot reach the ARIA API. Check that <code>aria-api.service</code> is running.
        </div>
      )}

      <div className="grid grid-cols-1 gap-3.5 lg:grid-cols-[1.4fr_1fr]">
        <Card title="Resident now">
          {running.length === 0 ? (
            <EmptyState>Nothing is resident. Start a model from Operate.</EmptyState>
          ) : (
            <ul className="m-0 list-none p-0">
              {running.map((s) => {
                const cpu = /cpu/i.test(s.backend_device || '')
                return (
                  <li
                    key={s.slug}
                    className="flex items-center gap-2.5 border-b border-line py-2 last:border-b-0"
                  >
                    <StatusDot state="running" />
                    <span className="min-w-0 flex-1 truncate text-xs">{s.slug}</span>
                    <span className="tnum text-[11px] text-ink-dim">
                      {cpu ? 'cpu' : `${(s.resident_gib_estimate ?? 0).toFixed(1)} GiB`}
                    </span>
                  </li>
                )
              })}
            </ul>
          )}
        </Card>

        <Card title="Memory budget">
          <Meter
            segments={[{ key: 'used', pct: total ? (used / total) * 100 : 0, color: 'bg-live' }]}
            left={`${used.toFixed(1)} used`}
            right={`${total.toFixed(1)} GiB`}
          />
          <p className="mt-2.5 font-sans text-xs leading-relaxed text-ink-dim">
            The large models are mutually exclusive — only one fits at a time.{' '}
            <Link href="/operate" className="text-accent underline underline-offset-2">
              Operate
            </Link>{' '}
            shows what a swap would cost.
          </p>
        </Card>
      </div>

      <div className="mt-3.5 grid grid-cols-1 gap-3.5 sm:grid-cols-2 xl:grid-cols-4">
        {AREAS.map((a) => (
          <Link
            key={a.href}
            href={a.href}
            className="rounded border border-line bg-panel p-3.5 transition-colors hover:border-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent"
          >
            <div className="text-[10px] uppercase tracking-[0.16em] text-accent">{a.label}</div>
            <p className="mt-1.5 font-sans text-xs leading-relaxed text-ink-dim">{a.blurb}</p>
          </Link>
        ))}
      </div>
    </AppShell>
  )
}
