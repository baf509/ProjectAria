'use client'

/**
 * ARIA - /supervise/shells master/detail frame
 *
 * Master/detail is ROUTING, not state (contract rule 8): the list lives here,
 * the terminal in [name]/page.tsx, and `useSelectedLayoutSegment` hides the
 * list below lg when a detail is open — so tapping a shell on the phone is a
 * real navigation with a real Back, instead of the old `useState(selected)`
 * whose "detail" rendered 1.2kpx below the fold and appeared to do nothing.
 *
 * The shell is `flush`: the old page sized its split against chrome that no
 * longer exists (`md:h-[calc(100vh-72px)]`, `max-h-[40vh]` list, `min-h-[60vh]`
 * terminal); here height is the shell's `--vvh` flex chain all the way down.
 */
import { ReactNode } from 'react'
import { useSelectedLayoutSegment } from 'next/navigation'
import { AppShell, StatusStat } from '@/components/shell/AppShell'
import { ShellList } from '@/features/shells/ShellList'
import { useResource } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type { FleetOverviewPayload } from '@/lib/api/types'

export default function ShellsLayout({ children }: { children: ReactNode }) {
  const segment = useSelectedLayoutSegment()
  const selected = segment ? decodeURIComponent(segment) : null
  // Same key ShellList uses — one request, one cache entry.
  const overview = useResource<FleetOverviewPayload>(K.shellsOverview, { tier: 'fast' })
  const d = overview.data

  return (
    <AppShell
      flush
      width="flush"
      // The fleet namespace prefixes everything with `claude-`; the short name
      // is what identifies the shell.
      title={selected ? selected.replace(/^claude-/, '') : undefined}
      back={selected ? { href: '/supervise/shells', label: 'Shells' } : undefined}
      status={
        <>
          <StatusStat label="LIVE">{d?.active_count ?? '—'}</StatusStat>
          <StatusStat label="AWAITING" tone={(d?.awaiting_count ?? 0) > 0 ? 'warn' : 'default'}>
            {d?.awaiting_count ?? '—'}
          </StatusStat>
          <StatusStat label="BLOCKED" tone={(d?.blocked_count ?? 0) > 0 ? 'warn' : 'default'}>
            {d?.blocked_count ?? '—'}
          </StatusStat>
        </>
      }
    >
      <div className="flex min-h-0 w-full flex-1">
        <aside
          aria-label="Watched shells"
          className={`${selected ? 'hidden lg:flex' : 'flex'} min-h-0 w-full flex-col lg:w-80 lg:shrink-0 lg:border-r lg:border-line`}
        >
          <ShellList />
        </aside>
        <section className={`${selected ? 'flex' : 'hidden lg:flex'} min-h-0 min-w-0 flex-1 flex-col`}>
          {children}
        </section>
      </div>
    </AppShell>
  )
}
