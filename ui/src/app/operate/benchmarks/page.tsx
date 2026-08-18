'use client'

/**
 * ARIA - Benchmarks (/operate/benchmarks)
 *
 * Pick suites + targets, launch an evalstack run, and read past runs. Replaces
 * /dashboard/benchmarks, which predated the UI primitives entirely (0 imports
 * from components/ui, its own translucent panels, and a `bg-live text-ink`
 * start button that was illegible in the light theme).
 *
 * Non-obvious choices:
 *  - runs poll at 'live' ONLY while one is running, else 'lazy' — the list is
 *    append-only history, and the 5s cadence exists solely for the in-flight
 *    log tail/status;
 *  - run rows are two-line Rows (status chip + id / suites→targets clamped):
 *    the old flex row had no shrink-0/min-w-0/truncate discipline, so the run
 *    id broke at its hyphen and the age wrapped into a column at 390px;
 *  - a run row is a <Link> to /operate/benchmarks/[id], not onClick state —
 *    the selected run survives reload and Back (contract rule 8).
 */
import { useEffect, useState } from 'react'
import Link from 'next/link'
import { X } from 'lucide-react'
import { AppShell, StatusStat } from '@/components/shell/AppShell'
import { Card, Chip, EmptyState, Notice } from '@/components/ui/primitives'
import { Button, ConfirmButton, IconButton, Toasts, type Toast } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Stack, Columns, Row } from '@/components/layout'
import { useAction, useResource } from '@/lib/swr'
import { K, dismissBenchRun, dismissFinishedBenchRuns } from '@/lib/api/endpoints'
import type { BenchHealth, BenchRunList, BenchSuitesResponse, BenchTargetsResponse } from '@/lib/api/types'
import { LaunchForm } from '@/features/benchmarks/LaunchForm'
import { RUN_TONE, runDuration, startedMs } from '@/features/benchmarks/lib'
import { relativeTime } from '@/lib/time'

function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([])
  const push = (tone: Toast['tone'], text: string) => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, tone, text }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000)
  }
  return { toasts, push, dismiss: (id: number) => setToasts((t) => t.filter((x) => x.id !== id)) }
}


/**
 * Per-run dismissal. Two-tap (the armed state says the word, per the Converse
 * delete lesson: an icon swap alone reads as "nothing happened").
 */
function DismissRun({ runId, onDone }: { runId: string; onDone: () => void }) {
  const run = useAction()
  const [armed, setArmed] = useState(false)
  const [busy, setBusy] = useState(false)

  if (!armed) {
    return (
      <IconButton label={`Dismiss run ${runId}`} onClick={() => setArmed(true)} className="shrink-0">
        <X size={16} aria-hidden="true" />
      </IconButton>
    )
  }
  return (
    <Button
      variant="danger"
      busy={busy}
      className="shrink-0"
      onClick={async () => {
        setBusy(true)
        await run(() => dismissBenchRun(runId), { invalidate: ['/benchmarks'] })
        setBusy(false)
        setArmed(false)
        onDone()
      }}
    >
      Dismiss?
    </Button>
  )
}

export default function BenchmarksPage() {
  const { toasts, push, dismiss } = useToasts()

  const health = useResource<BenchHealth>(K.benchHealth, { tier: 'lazy' })
  const suites = useResource<BenchSuitesResponse>(K.benchSuites, { tier: 'static' })
  const targets = useResource<BenchTargetsResponse>(K.benchTargets, { tier: 'static' })

  // Tier follows the data one render behind: state flips after the payload
  // says a run is in flight, which is the earliest any poll could matter.
  const [anyRunning, setAnyRunning] = useState(false)
  const runs = useResource<BenchRunList>(K.benchRuns(25), { tier: anyRunning ? 'live' : 'lazy' })
  const clear = useAction()
  const runningNow = (runs.data?.runs ?? []).some((r) => r.status === 'running')
  useEffect(() => setAnyRunning(runningNow), [runningNow])

  const budget = targets.data?.gpu_budget_gb ?? health.data?.gpu_budget_gb ?? 0
  const unavailable = health.data && !health.data.available

  return (
    <AppShell
      title="Benchmarks"
      status={
        <>
          <StatusStat label="RUNNING" tone={runningNow ? 'ok' : 'default'}>
            {runningNow ? 1 : 0}
          </StatusStat>
          <StatusStat label="RUNS">{runs.data?.runs?.length ?? '…'}</StatusStat>
          <StatusStat label="BUDGET">{budget ? `${budget} GB` : '…'}</StatusStat>
        </>
      }
    >
      <Stack>
        {unavailable && (
          <Notice tone="warn">
            Benchmark harness unavailable: evalstack not found at <code className="break-all">{health.data?.root}</code>
          </Notice>
        )}

        <Columns lg={2}>
          {!unavailable && (
            <Card title="Launch" hint="evalstack suites × targets">
              <Async r={suites} skeletonRows={5}>
                {(s) => (
                  <Async r={targets} skeletonRows={5}>
                    {(t) => (
                      <LaunchForm
                        suites={s.suites}
                        targets={t.targets}
                        budget={budget}
                        runInFlight={runningNow}
                        onError={(m) => push('warn', m)}
                      />
                    )}
                  </Async>
                )}
              </Async>
            </Card>
          )}

          <Card
            title="Runs"
            hint="newest first"
            actions={
              (runs.data?.runs ?? []).some((r) => r.status !== 'running') ? (
                <ConfirmButton
                  label="Clear finished"
                  confirmLabel="Clear all finished runs?"
                  variant="default"
                  onConfirm={async () => {
                    await clear(() => dismissFinishedBenchRuns(0), { invalidate: ['/benchmarks'] })
                    void runs.refresh()
                  }}
                />
              ) : undefined
            }
          >
            <Async r={runs} skeletonRows={6} isEmpty={(d) => (d.runs?.length ?? 0) === 0} empty="No runs yet.">
              {(d) => (
                <ul className="m-0 list-none p-0">
                  {d.runs.map((r) => (
                    <li key={r.run_id} className="flex min-w-0 items-center gap-1 border-b border-line last:border-b-0">
                      <Row
                        as={Link}
                        href={`/operate/benchmarks/${encodeURIComponent(r.run_id)}`}
                        marker={<Chip tone={RUN_TONE[r.status] ?? 'neutral'}>{r.status}</Chip>}
                        trailing={`${runDuration(r)} · ${relativeTime(startedMs(r))}`}
                        className="min-w-0 flex-1 py-1.5 hover:bg-panel-2"
                      >
                        <span className="block min-w-0">
                          <span className="block break-all font-mono text-body text-ink">{r.run_id}</span>
                          <span className="line-clamp-1 wrap-anywhere text-micro text-ink-faint">
                            {r.suites.join('+')} → {r.targets.join(', ')}
                          </span>
                        </span>
                      </Row>
                      {/* Sibling, not nested: a control inside the row's Link
                          would navigate instead of acting. */}
                      {r.status !== 'running' && (
                        <DismissRun runId={r.run_id} onDone={() => void runs.refresh()} />
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </Async>
          </Card>
        </Columns>
      </Stack>
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </AppShell>
  )
}
