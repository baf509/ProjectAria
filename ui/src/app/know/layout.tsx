'use client'

/**
 * ARIA - Know: layout (segment sub-nav)
 *
 * The old /dashboard was ONE 1350-line client page holding ten useState tabs
 * and fetching 14 endpoints in a single startTransition — every tab was blank
 * until the slowest call (the 8.8s model-servers inspect) returned. The tabs
 * are route segments now: each mounts only its own 1–3 resources, the URL is
 * the tab (deep-linkable, Back works), and this layout contributes only the
 * shell and the TabStrip.
 */
import { ReactNode, useCallback, useState } from 'react'
import { useRouter, useSelectedLayoutSegment } from 'next/navigation'
import { AppShell, StatusStat } from '@/components/shell/AppShell'
import { TabStrip } from '@/components/ui/controls'
import { KnowStatusProvider, type KnowStat } from '@/features/know/knowStatus'

const SEGMENTS = [
  { key: 'memories', label: 'Memories' },
  { key: 'tasks', label: 'Tasks' },
  { key: 'research', label: 'Research' },
  { key: 'workflows', label: 'Workflows' },
  { key: 'usage', label: 'Usage' },
  { key: 'agents', label: 'Agents' },
]

export default function KnowLayout({ children }: { children: ReactNode }) {
  const router = useRouter()
  const segment = useSelectedLayoutSegment() ?? 'memories'
  const [stats, setStats] = useState<KnowStat[]>([])
  const onStats = useCallback((next: KnowStat[]) => setStats(next), [])

  return (
    <AppShell
      status={stats.map((s) => (
        <StatusStat key={s.label} label={s.label} tone={s.tone}>
          {s.value}
        </StatusStat>
      ))}
    >
      <div className="flex min-w-0 flex-col gap-gap">
        <TabStrip items={SEGMENTS} active={segment} onSelect={(key) => router.push(`/know/${key}`)} />
        <KnowStatusProvider onStats={onStats}>{children}</KnowStatusProvider>
      </div>
    </AppShell>
  )
}
