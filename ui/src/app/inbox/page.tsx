'use client'

import { AppShell, StatusStat } from '@/components/shell/AppShell'
import { InboxLanes } from '@/features/inbox/InboxLanes'
import { useResource } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type { AlertsResponse, ReviewResponse } from '@/lib/api/types'

export default function InboxPage() {
  const alerts = useResource<AlertsResponse>(K.alerts(undefined, 200), { tier: 'normal' })
  const review = useResource<ReviewResponse>(K.review(200), { tier: 'lazy' })
  const needsHuman = (alerts.data?.alerts ?? []).filter((a) => a.needs_human).length
  const waiting = alerts.data?.alerts?.length ?? 0

  return (
    <AppShell
      status={
        <>
          <StatusStat label="NEEDS YOU" tone={needsHuman > 0 ? 'warn' : 'default'}>
            {needsHuman}
          </StatusStat>
          <StatusStat label="WAITING">{waiting}</StatusStat>
          <StatusStat label="REVIEW">{review.data?.count ?? review.data?.items?.length ?? 0}</StatusStat>
        </>
      }
    >
      <InboxLanes />
    </AppShell>
  )
}
