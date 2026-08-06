/**
 * Autonomy — what ARIA did without being asked.
 *
 * dreams, awareness and heartbeat had ~17 endpoints between them and no UI at
 * all, so the only evidence any of it ran was in Mongo. Reviewing unprompted
 * activity is its own posture — slow, retrospective, occasionally requiring a
 * decision — which is why it is an area rather than another dashboard section.
 *
 * Soul proposals are shown here with full context, but their approve/reject
 * also appears in the Inbox: this is where you read them, that is where you
 * clear them.
 */
'use client'

import Link from 'next/link'
import { useCallback, useEffect, useState } from 'react'
import { AppShell, StatusStat } from '@/components/AppShell'
import { Button, Card, EmptyState, ScrollX, StatusDot } from '@/components/ui'
import {
  autonomyApi,
  type AwarenessStatus,
  type DreamStatus,
  type JournalEntry,
  type Observation,
  type SoulProposal,
} from '@/lib/api-client-autonomy'

const SEVERITY: Record<string, string> = {
  critical: 'text-gone',
  error: 'text-gone',
  warning: 'text-accent',
  warn: 'text-accent',
  info: 'text-ink-dim',
  debug: 'text-ink-faint',
}

function ago(v?: string | null) {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return String(v)
  const mins = Math.round((Date.now() - d.getTime()) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`
  return `${Math.round(mins / 1440)}d ago`
}

export default function AutonomyPage() {
  const [dream, setDream] = useState<DreamStatus | null>(null)
  const [aware, setAware] = useState<AwarenessStatus | null>(null)
  const [beat, setBeat] = useState<any>(null)
  const [journal, setJournal] = useState<JournalEntry[]>([])
  const [obs, setObs] = useState<Observation[]>([])
  const [proposals, setProposals] = useState<SoulProposal[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [errors, setErrors] = useState<string[]>([])

  const load = useCallback(async () => {
    const r = await Promise.allSettled([
      autonomyApi.dreamStatus(),
      autonomyApi.awarenessStatus(),
      autonomyApi.heartbeatStatus(),
      autonomyApi.journal(10),
      autonomyApi.observations(30),
      autonomyApi.soulProposals(),
    ])
    const errs: string[] = []
    if (r[0].status === 'fulfilled') setDream(r[0].value)
    else errs.push('dreams')
    if (r[1].status === 'fulfilled') setAware(r[1].value)
    else errs.push('awareness')
    if (r[2].status === 'fulfilled') setBeat(r[2].value)
    else errs.push('heartbeat')
    if (r[3].status === 'fulfilled') setJournal(r[3].value ?? [])
    if (r[4].status === 'fulfilled') setObs(r[4].value ?? [])
    if (r[5].status === 'fulfilled') setProposals((r[5].value ?? []).filter((p) => p.status === 'pending'))
    setErrors(errs)
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  async function act(key: string, fn: () => Promise<unknown>) {
    setBusy(key)
    try {
      await fn()
      await load()
    } catch (e: any) {
      setErrors((p) => [...p, e?.message || 'Action failed'])
    } finally {
      setBusy(null)
    }
  }

  return (
    <AppShell
      area="Autonomy"
      status={
        <>
          <StatusStat label="DREAMS">{dream ? (dream.running ? 'running' : dream.enabled ? 'idle' : 'off') : '…'}</StatusStat>
          <StatusStat label="AWARENESS">{aware ? (aware.running ? 'running' : aware.enabled ? 'idle' : 'off') : '…'}</StatusStat>
          <StatusStat label="PENDING">{proposals.length}</StatusStat>
        </>
      }
    >
      {errors.length > 0 && (
        <div className="mb-3.5 rounded border border-gone bg-gone/10 px-3.5 py-2.5 font-sans text-xs">
          Unavailable: {Array.from(new Set(errors)).join(', ')}
        </div>
      )}

      <div className="grid grid-cols-1 gap-3.5 xl:grid-cols-[1.3fr_1fr]">
        <div className="min-w-0">
          {proposals.length > 0 && (
            <Card
              title="Soul proposals"
              hint="· changes ARIA wants to make to itself"
              className="mb-3.5"
              bodyClassName=""
            >
              <ul className="m-0 list-none p-0">
                {proposals.map((p) => (
                  <li key={p.id} className="border-b border-line px-3.5 py-3 last:border-b-0">
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="text-xs">
                        {p.proposals?.length ?? 0} proposed change
                        {(p.proposals?.length ?? 0) === 1 ? '' : 's'}
                      </span>
                      <span className="tnum text-[11px] text-ink-faint">{ago(p.created_at)}</span>
                    </div>
                    <ScrollX>
                      <pre className="mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded-sm bg-panel-2 p-2.5 text-[11px] leading-relaxed text-ink-dim">
                        {JSON.stringify(p.proposals, null, 2)}
                      </pre>
                    </ScrollX>
                    <div className="mt-2.5 flex gap-2">
                      <Button
                        variant="primary"
                        busy={busy === 'ap' + p.id}
                        onClick={() => act('ap' + p.id, () => autonomyApi.approveProposal(p.id))}
                      >
                        Approve
                      </Button>
                      <Button
                        variant="danger"
                        busy={busy === 'rp' + p.id}
                        onClick={() => act('rp' + p.id, () => autonomyApi.rejectProposal(p.id))}
                      >
                        Reject
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Card title="Dream journal" hint="· unprompted reflection" bodyClassName="">
            {journal.length === 0 ? (
              <div className="p-3.5">
                <EmptyState>No journal entries yet.</EmptyState>
              </div>
            ) : (
              <ul className="m-0 list-none p-0">
                {journal.map((e) => (
                  <li key={e.id} className="border-b border-line px-3.5 py-3 last:border-b-0">
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="text-[11px] text-ink-faint">
                        {e.connections.length} connections · {e.knowledge_gaps.length} gaps ·{' '}
                        {e.memory_consolidations_proposed} consolidations
                      </span>
                      <span className="tnum text-[11px] text-ink-faint">{ago(e.created_at)}</span>
                    </div>
                    <p className="mt-1.5 line-clamp-4 whitespace-pre-wrap font-sans text-xs leading-relaxed text-ink-dim">
                      {e.journal_entry}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <div className="min-w-0">
          <Card title="Dreams">
            {!dream ? (
              <EmptyState>Status unavailable.</EmptyState>
            ) : (
              <>
                <ul className="m-0 list-none p-0 text-xs">
                  <Row k="Enabled" v={String(dream.enabled)} />
                  <Row k="In active hours" v={String(dream.is_active_hours)} />
                  <Row k="Interval" v={`${dream.interval_hours}h`} />
                  <Row k="Model" v={dream.claude_model} />
                  <Row k="Last run" v={ago(dream.last_run)} />
                  <Row k="Last status" v={dream.last_status ?? '—'} />
                </ul>
                <div className="mt-3">
                  <Button busy={busy === 'dream'} onClick={() => act('dream', autonomyApi.triggerDream)}>
                    Trigger dream
                  </Button>
                </div>
              </>
            )}
          </Card>

          <Card title="Awareness" className="mt-3.5">
            {!aware ? (
              <EmptyState>Status unavailable.</EmptyState>
            ) : (
              <>
                <ul className="m-0 list-none p-0 text-xs">
                  <Row k="Enabled" v={String(aware.enabled)} />
                  <Row k="Sensors" v={aware.sensors.join(', ') || '—'} />
                  <Row k="Poll every" v={`${aware.poll_interval_seconds}s`} />
                  <Row k="Analyse every" v={`${aware.analysis_interval_minutes}m`} />
                  <Row k="Last poll" v={ago(aware.last_poll)} />
                  <Row k="Last analysis" v={ago(aware.last_analysis)} />
                </ul>
                <div className="mt-3 flex gap-2">
                  <Button busy={busy === 'poll'} onClick={() => act('poll', autonomyApi.poll)}>
                    Poll now
                  </Button>
                  <Button busy={busy === 'analyze'} onClick={() => act('analyze', autonomyApi.analyze)}>
                    Analyse
                  </Button>
                </div>
              </>
            )}
          </Card>

          <Card title="Heartbeat" className="mt-3.5">
            {!beat ? (
              <EmptyState>Status unavailable.</EmptyState>
            ) : (
              <>
                <ul className="m-0 list-none p-0 text-xs">
                  {Object.entries(beat)
                    .filter(([, v]) => typeof v !== 'object')
                    .slice(0, 6)
                    .map(([k, v]) => (
                      <Row key={k} k={k.replace(/_/g, ' ')} v={String(v)} />
                    ))}
                </ul>
                <div className="mt-3">
                  <Button busy={busy === 'beat'} onClick={() => act('beat', autonomyApi.heartbeatTrigger)}>
                    Trigger heartbeat
                  </Button>
                </div>
              </>
            )}
          </Card>
        </div>
      </div>

      <Card title="Observations" hint="· what the sensors noticed" className="mt-3.5" bodyClassName="">
        {obs.length === 0 ? (
          <div className="p-3.5">
            <EmptyState>
              No observations recorded. Awareness polls on an interval — use “Poll now” to force one.
            </EmptyState>
          </div>
        ) : (
          <ul className="m-0 list-none p-0">
            {obs.map((o, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line px-3.5 py-2 last:border-b-0">
                <StatusDot state={o.severity === 'critical' || o.severity === 'error' ? 'absent' : 'exited'} />
                <span className="text-[11px] text-ink-faint">{o.sensor}</span>
                <span className={`text-[11px] ${SEVERITY[o.severity] ?? 'text-ink-dim'}`}>{o.severity}</span>
                <span className="min-w-0 flex-1 truncate text-xs" title={o.detail ?? o.summary}>
                  {o.summary}
                </span>
                <span className="tnum shrink-0 text-[11px] text-ink-faint">{ago(o.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <p className="mt-3.5 font-sans text-[11px] text-ink-faint">
        Decisions raised here also appear in{' '}
        <Link href="/inbox" className="text-accent underline underline-offset-2">
          Inbox
        </Link>
        .
      </p>
    </AppShell>
  )
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <li className="flex items-center justify-between gap-3 border-b border-line py-1.5 last:border-b-0">
      <span className="text-[10px] uppercase tracking-[0.06em] text-ink-faint">{k}</span>
      <span className="tnum min-w-0 truncate text-right text-xs" title={v}>
        {v}
      </span>
    </li>
  )
}
