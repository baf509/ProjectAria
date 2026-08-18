'use client'

/**
 * ARIA - Benchmark run detail (/operate/benchmarks/[id])
 *
 * The run id lives in the URL, not component state — the old page kept the
 * selected run in `useState(active)`, so reload and Back both lost it and the
 * phone appeared to "do nothing" when a row was tapped (the detail rendered
 * 1.2kpx below the fold).
 *
 * Polls at 'live' only while this run is running (the log tail is the one
 * thing that changes); finished runs are immutable history and sit at 'lazy'.
 */
import { useEffect, useState } from 'react'
import Link from 'next/link'
import { AppShell, StatusStat } from '@/components/shell/AppShell'
import { Card, Chip, EmptyState, KeyValue } from '@/components/ui/primitives'
import { ConfirmButton, Toasts, type Toast } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Stack, ScrollX } from '@/components/layout'
import { useResource, useAction } from '@/lib/swr'
import { K, cancelBenchRun } from '@/lib/api/endpoints'
import type { BenchRun } from '@/lib/api/types'
import { RUN_TONE, runDuration, startedMs } from '@/features/benchmarks/lib'
import { absoluteTime, relativeTime } from '@/lib/time'

function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([])
  const push = (tone: Toast['tone'], text: string) => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, tone, text }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000)
  }
  return { toasts, push, dismiss: (id: number) => setToasts((t) => t.filter((x) => x.id !== id)) }
}

export default function BenchRunPage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id)
  const { toasts, push, dismiss } = useToasts()
  const runAction = useAction()

  // Tier follows the data one render behind (same pattern as the list page).
  const [live, setLive] = useState(false)
  const run = useResource<BenchRun>(K.benchRun(id), { tier: live ? 'live' : 'lazy' })
  const isRunning = run.data?.status === 'running'
  useEffect(() => setLive(!!isRunning), [isRunning])

  return (
    <AppShell
      title={id}
      back={{ href: '/operate/benchmarks', label: 'Benchmarks' }}
      status={
        run.data ? (
          <>
            <StatusStat
              label="STATUS"
              tone={run.data.status === 'succeeded' ? 'ok' : run.data.status === 'running' ? 'default' : 'warn'}
            >
              {run.data.status}
            </StatusStat>
            <StatusStat label="ELAPSED">{runDuration(run.data)}</StatusStat>
          </>
        ) : undefined
      }
    >
      <Stack>
        {/* The top-bar back chip is phone-only; give the laptop a way home too. */}
        <Link
          href="/operate/benchmarks"
          data-inline
          className="hidden self-start text-micro text-ink-dim underline underline-offset-2 hover:text-ink lg:inline"
        >
          ← All runs
        </Link>

        <Async r={run} skeletonRows={6}>
          {(r) => (
            <>
              <Card
                title="Run"
                actions={
                  r.status === 'running' ? (
                    <ConfirmButton
                      label="Cancel run"
                      onConfirm={async () => {
                        const ok = await runAction(() => cancelBenchRun(id), {
                          invalidate: ['/benchmarks'],
                          onError: (e) => push('warn', e.message),
                        })
                        if (ok !== undefined) push('ok', 'Cancel requested')
                      }}
                    />
                  ) : undefined
                }
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <Chip tone={RUN_TONE[r.status] ?? 'neutral'}>{r.status}</Chip>
                  <span className="break-all font-mono text-body text-ink">{r.run_id}</span>
                  <span className="ml-auto shrink-0 text-micro text-ink-faint">{relativeTime(startedMs(r))}</span>
                </div>
                <KeyValue
                  layout="stack"
                  items={[
                    { k: 'Suites', v: r.suites.join(', '), kind: 'prose' },
                    { k: 'Targets', v: r.targets.join(', '), kind: 'prose' },
                    { k: 'Limit', v: r.limit ?? 'all', kind: 'num' },
                    { k: 'Started', v: absoluteTime(startedMs(r)) },
                    { k: 'Duration', v: runDuration(r), kind: 'num' },
                    ...(r.returncode !== null && r.returncode !== undefined
                      ? [{ k: 'Return code', v: String(r.returncode), kind: 'num' as const }]
                      : []),
                    ...(r.results_dir ? [{ k: 'Results dir', v: r.results_dir, kind: 'ident' as const }] : []),
                  ]}
                />
              </Card>

              <Card title={`Metrics · ${r.metrics?.length ?? 0}`}>
                {r.metrics?.length ? (
                  <ScrollX>
                    <table className="w-full border-collapse">
                      <thead>
                        <tr className="text-left text-micro uppercase tracking-[0.08em] text-ink-faint">
                          <th className="py-1.5 pr-4 font-medium">target</th>
                          <th className="py-1.5 pr-4 font-medium">benchmark</th>
                          <th className="py-1.5 pr-4 font-medium">metric</th>
                          <th className="py-1.5 text-right font-medium">value</th>
                        </tr>
                      </thead>
                      <tbody className="font-mono text-label">
                        {r.metrics.map((m, i) => (
                          <tr key={i} className="border-t border-line">
                            <td className="py-1.5 pr-4">{m.target}</td>
                            <td className="py-1.5 pr-4 text-ink-dim">{m.benchmark}</td>
                            <td className="py-1.5 pr-4 text-ink-dim">{m.metric}</td>
                            <td className="tnum py-1.5 text-right">
                              {typeof m.value === 'number' ? m.value.toFixed(3) : String(m.value ?? '—')}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </ScrollX>
                ) : (
                  <EmptyState>
                    {r.status === 'running' ? 'No metrics yet — the run is still in flight.' : 'No metrics recorded.'}
                  </EmptyState>
                )}
              </Card>

              <Card title="Log tail" hint={r.log}>
                {r.log_tail ? (
                  // Wrapped, not a nested two-axis scroller: the page scrolls.
                  <pre className="m-0 whitespace-pre-wrap break-words rounded-sm bg-panel-2 p-2.5 font-mono text-micro leading-relaxed text-ink-dim">
                    {r.log_tail}
                  </pre>
                ) : (
                  <EmptyState>No log output captured.</EmptyState>
                )}
              </Card>
            </>
          )}
        </Async>
      </Stack>
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </AppShell>
  )
}
