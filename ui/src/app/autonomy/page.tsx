'use client'

/**
 * ARIA - Autonomy: what ARIA did without being asked.
 *
 * dreams, awareness and heartbeat have ~17 endpoints between them; reviewing
 * unprompted activity is its own posture — slow, retrospective, occasionally
 * requiring a decision — which is why this is an area, not a dashboard tab.
 *
 * Rebuilt on the responsive foundation (2026-08-17 audit: 162 of 232 text
 * nodes under 12px, hand-rolled 30s setInterval that never paused when
 * hidden, observations truncated behind hover `title=` which reveals nothing
 * on touch):
 *  - data comes from useResource (statuses at 'slow', history at 'lazy' —
 *    journal entries arrive every 6h, nothing here needs a 5s cadence);
 *  - status cards are KeyValue, journal entries and observations are
 *    Disclosure rows (observations grouped by sensor — 30 rows were mostly
 *    the same two sensors repeating);
 *  - soul proposals render through SoulProposalCard (wrapped prose + honest
 *    force-approve for stale ones) instead of raw JSON in a nested scroller.
 *
 * Soul proposals also surface in the Inbox: this is where you read them with
 * full context, that is where you clear them.
 */
import { useMemo, useState } from 'react'
import Link from 'next/link'
import { AppShell, StatusStat } from '@/components/shell/AppShell'
import { Card, Chip, EmptyState, KeyValue } from '@/components/ui/primitives'
import { Button, Disclosure, Toasts, type Toast } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Stack, Cluster, Columns } from '@/components/layout'
import { useResource, useAction } from '@/lib/swr'
import {
  K,
  triggerDream,
  pollAwareness,
  analyzeAwareness,
  triggerHeartbeat,
} from '@/lib/api/endpoints'
import type {
  AwarenessStatus,
  DreamStatus,
  HeartbeatStatus,
  JournalEntry,
  Observation,
  SoulProposalDetail,
} from '@/lib/api/types'
import { SoulProposalCard } from '@/features/autonomy/SoulProposalCard'
import { relativeTime } from '@/lib/time'

const SEVERITY_TONE: Record<string, 'warn' | 'accent' | 'neutral'> = {
  critical: 'warn',
  error: 'warn',
  warning: 'accent',
  warn: 'accent',
  info: 'neutral',
  debug: 'neutral',
}

function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([])
  const push = (tone: Toast['tone'], text: string) => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, tone, text }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000)
  }
  return { toasts, push, dismiss: (id: number) => setToasts((t) => t.filter((x) => x.id !== id)) }
}

const onoff = (v?: boolean) => (v === undefined ? '—' : v ? 'yes' : 'no')

/* ---------------------------------------------------------------- journal */

function JournalRows({ entries }: { entries: JournalEntry[] }) {
  if (entries.length === 0) return <EmptyState>No journal entries yet.</EmptyState>
  return (
    <ul className="m-0 list-none p-0">
      {entries.map((e) => (
        <li key={e.id} className="border-b border-line py-1 last:border-b-0">
          <Disclosure
            summary={
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <span className="text-micro text-ink-faint">
                    {e.connection_count ?? e.connections?.length ?? 0} connections ·{' '}
                    {e.knowledge_gap_count ?? e.knowledge_gaps?.length ?? 0} gaps ·{' '}
                    {e.memory_consolidations_proposed ?? 0} consolidations
                  </span>
                  <span className="ml-auto shrink-0 text-micro text-ink-faint">{relativeTime(e.created_at)}</span>
                </span>
                <span className="line-clamp-2 min-w-0 wrap-anywhere font-sans text-prose text-ink-dim">
                  {e.journal_entry}
                </span>
              </span>
            }
          >
            {/* The full entry, wrapped — the page scrolls, nothing nested does. */}
            <p className="m-0 max-w-prose whitespace-pre-wrap wrap-anywhere font-sans text-prose leading-relaxed text-ink-dim">
              {e.journal_entry}
            </p>
          </Disclosure>
        </li>
      ))}
    </ul>
  )
}

/* ------------------------------------------------------------ observations */

function ObservationGroups({ observations }: { observations: Observation[] }) {
  // Grouped by sensor: a flat list of 30 was mostly `git` and `system`
  // repeating, burying the one filesystem event that mattered.
  const groups = useMemo(() => {
    const map = new Map<string, Observation[]>()
    for (const o of observations) {
      const key = o.sensor ?? 'unknown'
      const list = map.get(key) ?? []
      list.push(o)
      map.set(key, list)
    }
    return [...map.entries()].sort((a, b) => b[1].length - a[1].length)
  }, [observations])

  if (observations.length === 0) {
    return (
      <EmptyState>
        No observations recorded. Awareness polls on an interval — use “Poll now” to force one.
      </EmptyState>
    )
  }

  return (
    <Stack gap="sm">
      {groups.map(([sensor, group]) => {
        const worst = group.find((o) => SEVERITY_TONE[o.severity ?? ''] === 'warn')
          ? 'warn'
          : group.find((o) => SEVERITY_TONE[o.severity ?? ''] === 'accent')
            ? 'accent'
            : 'neutral'
        return (
          <Disclosure
            key={sensor}
            summary={
              <span className="flex min-w-0 flex-wrap items-center gap-2">
                <Chip tone={worst as 'warn' | 'accent' | 'neutral'}>{sensor}</Chip>
                <span className="tnum text-micro text-ink-dim">{group.length}</span>
                <span className="ml-auto shrink-0 text-micro text-ink-faint">{relativeTime(group[0]?.created_at)}</span>
              </span>
            }
          >
            <ul className="m-0 list-none p-0">
              {group.map((o, i) => (
                <li key={i} className="border-b border-line py-1.5 last:border-b-0">
                  <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span
                      className={`text-micro ${
                        SEVERITY_TONE[o.severity ?? ''] === 'warn'
                          ? 'text-gone'
                          : SEVERITY_TONE[o.severity ?? ''] === 'accent'
                            ? 'text-accent'
                            : 'text-ink-faint'
                      }`}
                    >
                      {o.severity ?? 'info'}
                    </span>
                    <span className="text-micro text-ink-faint">{o.event_type}</span>
                    <span className="ml-auto shrink-0 text-micro text-ink-faint">{relativeTime(o.created_at)}</span>
                  </span>
                  <p className="m-0 mt-0.5 max-w-prose wrap-anywhere font-sans text-prose text-ink-dim">{o.summary}</p>
                  {o.detail && (
                    <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono text-micro leading-relaxed text-ink-faint">
                      {o.detail}
                    </pre>
                  )}
                </li>
              ))}
            </ul>
          </Disclosure>
        )
      })}
    </Stack>
  )
}

/* -------------------------------------------------------------------- page */

export default function AutonomyPage() {
  const { toasts, push, dismiss } = useToasts()
  const onDone = (t: string) => push('ok', t)
  const onError = (t: string) => push('warn', t)

  const dreams = useResource<DreamStatus>(K.dreamsStatus, { tier: 'slow' })
  const aware = useResource<AwarenessStatus>(K.awarenessStatus, { tier: 'slow' })
  const beat = useResource<HeartbeatStatus>(K.heartbeatStatus, { tier: 'slow' })
  const journal = useResource<JournalEntry[]>(K.dreamsJournal(10), { tier: 'lazy' })
  const observations = useResource<Observation[]>(K.observations(), { tier: 'lazy' })
  const proposals = useResource<SoulProposalDetail[]>(K.soulProposals, { tier: 'lazy' })

  const run = useAction()
  const [busy, setBusy] = useState<string | null>(null)

  const pending = (proposals.data ?? []).filter((p) => p.status === 'pending')

  async function act(key: string, fn: () => Promise<unknown>, done: string, invalidate: string[]) {
    setBusy(key)
    const ok = await run(fn, { invalidate, onError: (e) => onError(e.message) })
    setBusy(null)
    if (ok !== undefined) onDone(done)
  }

  const worker = (s?: { running?: boolean; enabled?: boolean }) =>
    !s ? '…' : s.running ? 'running' : s.enabled ? 'idle' : 'off'

  return (
    <AppShell
      status={
        <>
          <StatusStat label="DREAMS" tone={dreams.data?.running ? 'ok' : 'default'}>
            {worker(dreams.data)}
          </StatusStat>
          <StatusStat label="AWARENESS" tone={aware.data?.running ? 'ok' : 'default'}>
            {worker(aware.data)}
          </StatusStat>
          <StatusStat label="PENDING" tone={pending.length > 0 ? 'warn' : 'default'}>
            {pending.length}
          </StatusStat>
        </>
      }
    >
      <Stack>
        {pending.length > 0 && (
          <Card title={`Soul proposals · ${pending.length}`} hint="changes ARIA wants to make to itself">
            <Stack gap="sm">
              {pending.map((p) => (
                <SoulProposalCard key={p.id} proposal={p} onDone={onDone} onError={onError} />
              ))}
            </Stack>
          </Card>
        )}

        <Columns lg={3}>
          <Card title="Dreams">
            <Async r={dreams} skeletonRows={4}>
              {(d) => (
                <Stack gap="sm">
                  <KeyValue
                    items={[
                      { k: 'Enabled', v: onoff(d.enabled) },
                      { k: 'In active hours', v: onoff(d.is_active_hours) },
                      { k: 'Interval', v: `${d.interval_hours ?? '—'}h`, kind: 'num' },
                      { k: 'Model', v: d.claude_model ?? '—', kind: 'ident' },
                      { k: 'Last run', v: relativeTime(d.last_run) || 'never' },
                      { k: 'Last status', v: d.last_status ?? '—' },
                    ]}
                  />
                  <Button
                    busy={busy === 'dream'}
                    onClick={() => act('dream', triggerDream, 'Dream cycle triggered', ['/dreams'])}
                    className="self-start"
                  >
                    Trigger dream
                  </Button>
                </Stack>
              )}
            </Async>
          </Card>

          <Card title="Awareness">
            <Async r={aware} skeletonRows={4}>
              {(a) => (
                <Stack gap="sm">
                  <KeyValue
                    items={[
                      { k: 'Enabled', v: onoff(a.enabled) },
                      { k: 'Sensors', v: a.sensors?.join(', ') || '—', kind: 'prose' },
                      { k: 'Poll every', v: `${a.poll_interval_seconds ?? '—'}s`, kind: 'num' },
                      { k: 'Analyse every', v: `${a.analysis_interval_minutes ?? '—'}m`, kind: 'num' },
                      { k: 'Last poll', v: relativeTime(a.last_poll) || 'never' },
                      { k: 'Last analysis', v: relativeTime(a.last_analysis) || 'never' },
                    ]}
                  />
                  <Cluster>
                    <Button
                      busy={busy === 'poll'}
                      onClick={() => act('poll', pollAwareness, 'Poll requested', ['/awareness'])}
                    >
                      Poll now
                    </Button>
                    <Button
                      busy={busy === 'analyze'}
                      onClick={() => act('analyze', analyzeAwareness, 'Analysis requested', ['/awareness'])}
                    >
                      Analyse
                    </Button>
                  </Cluster>
                </Stack>
              )}
            </Async>
          </Card>

          <Card title="Heartbeat">
            <Async r={beat} skeletonRows={4}>
              {(h) => (
                <Stack gap="sm">
                  <KeyValue
                    items={[
                      { k: 'Enabled', v: onoff(h.enabled) },
                      { k: 'Running', v: onoff(h.running) },
                      { k: 'Interval', v: `${h.interval_minutes ?? '—'}m`, kind: 'num' },
                      { k: 'Backend', v: h.backend ?? '—', kind: 'ident' },
                      { k: 'Model', v: h.model ?? '—', kind: 'ident' },
                      { k: 'Last run', v: relativeTime(h.last_run) || 'never' },
                    ]}
                  />
                  <Button
                    busy={busy === 'beat'}
                    onClick={() => act('beat', triggerHeartbeat, 'Heartbeat triggered', ['/heartbeat'])}
                    className="self-start"
                  >
                    Trigger heartbeat
                  </Button>
                </Stack>
              )}
            </Async>
          </Card>
        </Columns>

        <Card title="Dream journal" hint="unprompted reflection">
          <Async r={journal} skeletonRows={3} isEmpty={(d) => d.length === 0} empty="No journal entries yet.">
            {(d) => <JournalRows entries={d} />}
          </Async>
        </Card>

        <Card title="Observations" hint="what the sensors noticed, grouped by sensor">
          <Async r={observations} skeletonRows={3}>
            {(d) => <ObservationGroups observations={d} />}
          </Async>
        </Card>

        <p className="m-0 font-sans text-micro text-ink-faint">
          Decisions raised here also appear in{' '}
          <Link href="/inbox" data-inline className="text-accent underline underline-offset-2">
            Inbox
          </Link>
          .
        </p>
      </Stack>
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </AppShell>
  )
}
