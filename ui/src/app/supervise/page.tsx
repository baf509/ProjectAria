'use client'

/**
 * ARIA - /supervise: attention feed over every project, plus the fleet strip.
 * Replaces /cockpit (redirected in next.config.js). The counters live in the
 * shell's status strip — the old serif "Coherence Cockpit" hero pushed the
 * first card to 41% of a phone viewport.
 */
import { AppShell, StatusStat } from '@/components/shell/AppShell'
import { ProjectsBoard } from '@/features/supervise/ProjectsBoard'
import { useResource } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type { FleetOverview, ProjectsOverview } from '@/lib/api/types'

export default function SupervisePage() {
  const overview = useResource<ProjectsOverview>(K.projectsOverview, { tier: 'slow' })
  const fleet = useResource<FleetOverview>(K.shellsOverview, { tier: 'fast' })

  const attn = (overview.data?.projects ?? []).filter((p) => (p.attention_score ?? 0) > 0).length
  const alerts = overview.data?.unacked_alerts_total ?? 0
  const blocked = (fleet.data?.blocked_count ?? 0) + (fleet.data?.awaiting_count ?? 0)
  const working = fleet.data?.active_count ?? 0

  return (
    <AppShell
      status={
        <>
          <StatusStat label="ATTN" tone={attn > 0 ? 'warn' : 'default'}>
            {attn}
          </StatusStat>
          <StatusStat label="ALERTS" tone={alerts > 0 ? 'warn' : 'default'}>
            {alerts}
          </StatusStat>
          <StatusStat label="BLOCKED" tone={blocked > 0 ? 'warn' : 'default'}>
            {blocked}
          </StatusStat>
          <StatusStat label="WORKING" tone={working > 0 ? 'ok' : 'default'}>
            {working}
          </StatusStat>
        </>
      }
    >
      <ProjectsBoard />
    </AppShell>
  )
}
