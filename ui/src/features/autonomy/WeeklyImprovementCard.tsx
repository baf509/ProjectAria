'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { api } from '@/lib/http'
import { Markdown } from '@/features/converse/Markdown'
import { Card, EmptyState, KeyValue } from '@/components/ui/primitives'
import { Button, Disclosure } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Cluster, Stack } from '@/components/layout'
import { useAction, useResource } from '@/lib/swr'

type Status = { enabled: boolean; configured: boolean; configuration_error?: string; schedule: string; next_run_at?: string; active_run_id?: string; last_run_id?: string }
type Run = { _id: string; status: string; coverage?: string; created_at: string; shell_name?: string; report?: string; publication?: { status: string; path?: string }; notification?: { status: string }; findings: { title: string; disposition: string }[] }

function Report({ id }: { id: string }) {
  const report = useResource<Run>(`/improve/runs/${id}`, { tier: 'slow' })
  return <Async r={report} skeletonRows={3}>{r => <Stack gap="sm">
    {r.shell_name && <Link href={`/supervise/shells/${encodeURIComponent(r.shell_name)}`}>Watch {r.shell_name}</Link>}
    <Markdown text={r.report || 'Review is in progress. The report will appear here.'} />
    <KeyValue items={[{ k: "Vault publication", v: r.publication?.status || 'pending' }]} />
    <KeyValue items={[{ k: "Signal summary", v: r.notification?.status || 'pending' }]} />
  </Stack>}</Async>
}

export function WeeklyImprovementCard() {
  const act = useAction()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get('improvement')
    if (id && /^[a-f0-9]{32}$/.test(id)) setSelected(id)
  }, [])
  async function request(path: string, body: unknown, success: string) {
    setBusy(true)
    setMessage('')
    const result = await act(() => api(path, { method: 'POST', body }), {
      invalidate: ['/improve'], onError: e => setMessage(e.message),
    })
    if (result !== undefined) setMessage(success)
    setBusy(false)
  }
  const status = useResource<Status>('/improve/weekly', { tier: 'slow' })
  const runs = useResource<Run[]>('/improve/runs?limit=10', { tier: 'slow' })
  return <Card title="Weekly improvement review" hint="Hermes, Aria and model configuration">
    <Stack gap="sm">
      <Async r={status} skeletonRows={2}>{s => <Stack gap="sm">
        <KeyValue items={[{ k: "Schedule", v: s.schedule }]} />
        <KeyValue items={[{ k: "State", v: s.active_run_id ? 'running' : s.enabled ? 'enabled' : 'disabled' }]} />
        <KeyValue items={[{ k: "Next run", v: s.next_run_at ? new Date(s.next_run_at).toLocaleString() : 'Not scheduled' }]} />
        <Cluster>
          <Button disabled={busy || !s.enabled || !s.configured || !!s.active_run_id}
            onClick={() => request('/improve/trigger', { report_only: false }, 'Review started.')}>
            Run review now
          </Button>
          <Button disabled={busy || !s.enabled || !s.configured || !!s.active_run_id}
            onClick={() => request('/improve/trigger', { report_only: true }, 'Report-only review started.')}>
            Report only
          </Button>
          {s.active_run_id && <Button disabled={busy}
            onClick={() => request(`/improve/runs/${s.active_run_id}/cancel`, {}, 'Cancellation requested.')}>
            Cancel review
          </Button>}
        </Cluster>
        {s.configuration_error && <p className="m-0 wrap-anywhere text-ink-dim">{s.configuration_error}</p>}
      </Stack>}</Async>
      {message && <p role="status" className="m-0 text-ink-dim">{message}</p>}
      {selected && <Report id={selected} />}
      <Async r={runs} skeletonRows={2}>{items => items.length ? <Stack gap="sm">{items.map(r =>
        <Disclosure key={r._id} summary={`${new Date(r.created_at).toLocaleString()} · ${r.status} · ${r.coverage || 'collecting'}`}>
          <Report id={r._id} />
        </Disclosure>)} </Stack> : <EmptyState>No weekly reviews yet.</EmptyState>}</Async>
    </Stack>
  </Card>
}
