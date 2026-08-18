'use client'

/**
 * ARIA - /supervise/projects/[slug]: one project's cockpit.
 * Replaces /cockpit/[slug] (redirected). Selection is ROUTING, not useState —
 * this page survives reload and Back, and the shell's back chip returns to the
 * project list on a phone.
 */
import { useParams } from 'next/navigation'
import { AppShell, StatusStat } from '@/components/shell/AppShell'
import { ProjectCockpitView } from '@/features/supervise/ProjectCockpitView'
import { useResource } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type { ProjectCockpit } from '@/lib/api/types'
import { usd } from '@/lib/format'

export default function ProjectCockpitPage() {
  const params = useParams<{ slug: string }>()
  const slug = typeof params?.slug === 'string' ? decodeURIComponent(params.slug) : ''
  // Same key as the feature below — one request, shared cache entry.
  const cockpit = useResource<ProjectCockpit>(slug ? K.projectCockpit(slug) : null, { tier: 'fast' })

  const a = cockpit.data?.attention
  const blocked = a?.blocked_shells ?? 0
  const alerts = a?.unacked_alerts ?? 0
  const working = (a?.working_shells ?? 0) + (a?.running_sessions ?? 0)

  return (
    <AppShell
      title={cockpit.data?.project?.name || slug}
      back={{ href: '/supervise', label: 'All projects' }}
      status={
        <>
          <StatusStat label="BLOCKED" tone={blocked > 0 ? 'warn' : 'default'}>
            {blocked}
          </StatusStat>
          <StatusStat label="ALERTS" tone={alerts > 0 ? 'warn' : 'default'}>
            {alerts}
          </StatusStat>
          <StatusStat label="WORKING" tone={working > 0 ? 'ok' : 'default'}>
            {working}
          </StatusStat>
          <StatusStat label="SPEND">{usd(cockpit.data?.budget?.cost ?? 0)}</StatusStat>
        </>
      }
    >
      <ProjectCockpitView slug={slug} />
    </AppShell>
  )
}
