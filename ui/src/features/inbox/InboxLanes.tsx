'use client'

/**
 * ARIA - Inbox
 *
 * The surface Ben reads from a phone, rebuilt around what Alerts v2 actually
 * models:
 *  - `needs_human` is the lane that matters. Everything else is cockpit noise
 *    and now sits behind a collapsed "FYI" disclosure instead of competing for
 *    the top of the list.
 *  - A decision is the unit of audit (`POST /alerts/{id}/decide`, APPLY /
 *    REJECT / STOP / HOLD / IGNORE). The old Inbox could only Ack, so the one
 *    action the alert system is built around was unreachable from the web.
 *  - Nothing is truncated behind a hover `title=` — there is no hover on touch.
 *    Titles clamp to two lines and every row expands in place.
 *  - The review queue is grouped by (source, kind): 126 of 128 rows are the
 *    same scan-worker event, and the old flat list buried everything else.
 */
import { useMemo, useState } from 'react'
import Link from 'next/link'
import { useResource, useAction } from '@/lib/swr'
import { K, ackAlert, decideAlert, ackReview, acceptTodo, dismissTodo, approveProposal, rejectProposal } from '@/lib/api/endpoints'
import type {
  Alert,
  AlertsResponse,
  DecisionValue,
  ReviewItem,
  ReviewResponse,
  SoulProposal,
  Todo,
  TodosResponse,
} from '@/lib/api/types'
import { Card, Chip, Notice, Text, EmptyState, KeyValue } from '@/components/ui/primitives'
import { Button, Disclosure, Toasts, type Toast } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Stack, Cluster } from '@/components/layout'
import { relativeTime } from '@/lib/time'

const SEVERITY_TONE: Record<string, 'warn' | 'accent' | 'neutral'> = {
  critical: 'warn',
  high: 'warn',
  medium: 'accent',
  low: 'neutral',
  info: 'neutral',
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

/* ------------------------------------------------------------------ alerts */

function AlertRow({
  alert,
  onDone,
  onError,
}: {
  alert: Alert
  onDone: (text: string) => void
  onError: (text: string) => void
}) {
  const run = useAction()
  const [busy, setBusy] = useState<string | null>(null)
  const proposal = alert.proposal
  const hasProposal = !!proposal && Object.keys(proposal).length > 0

  async function decide(action: DecisionValue) {
    setBusy(action)
    const ok = await run(() => decideAlert(alert.id, action), {
      invalidate: ['/alerts'],
      onError: (e) => onError(e.message),
    })
    setBusy(null)
    if (ok !== undefined) onDone(`${action} recorded`)
  }

  async function ack() {
    setBusy('ack')
    const ok = await run(() => ackAlert(alert.id), { invalidate: ['/alerts'], onError: (e) => onError(e.message) })
    setBusy(null)
    if (ok !== undefined) onDone('Acknowledged')
  }

  const title = alert.detail || alert.message || alert.event_type || 'Alert'

  return (
    <li className="border-b border-line py-2 last:border-b-0">
      <Disclosure
        summary={
          <span className="flex min-w-0 flex-col gap-1">
            <span className="flex min-w-0 flex-wrap items-center gap-1.5">
              <Chip tone={SEVERITY_TONE[alert.severity ?? 'info'] ?? 'neutral'}>
                {alert.kind || alert.source || 'alert'}
              </Chip>
              {(alert.occurrences ?? 1) > 1 && <Chip>×{alert.occurrences}</Chip>}
              {alert.project_slug && (
                <Link
                  href={`/supervise/projects/${alert.project_slug}`}
                  // An inline link inside a text run: WCAG 2.5.8 exempts these
                  // from the 44px minimum, and the gate honours data-inline.
                  data-inline
                  className="text-micro text-accent underline underline-offset-2"
                  onClick={(e) => e.stopPropagation()}
                >
                  {alert.project_slug}
                </Link>
              )}
              <span className="ml-auto shrink-0 text-micro text-ink-faint">{relativeTime(alert.created_at)}</span>
            </span>
            {/* Two lines, then a real expand — never truncate-with-tooltip. */}
            <span className="line-clamp-2 min-w-0 wrap-anywhere font-sans text-prose text-ink">{title}</span>
          </span>
        }
      >
        <Stack gap="sm">
          <Text>{alert.detail || alert.message}</Text>

          {hasProposal && (
            <div className="rounded-sm border border-line bg-panel-2 p-2.5">
              <p className="m-0 text-micro uppercase tracking-[0.1em] text-ink-faint">Proposal</p>
              <KeyValue
                layout="stack"
                items={[
                  ...(proposal?.root_cause ? [{ k: 'Root cause', v: proposal.root_cause, kind: 'prose' as const }] : []),
                  ...(proposal?.fix ? [{ k: 'Fix', v: proposal.fix, kind: 'prose' as const }] : []),
                  ...(proposal?.action ? [{ k: 'Action', v: proposal.action, kind: 'ident' as const }] : []),
                  ...(proposal?.reason ? [{ k: 'Reason', v: proposal.reason, kind: 'prose' as const }] : []),
                  ...(proposal?.confidence !== undefined
                    ? [{ k: 'Confidence', v: `${Math.round((proposal.confidence ?? 0) * 100)}%`, kind: 'num' as const }]
                    : []),
                  ...(proposal?.evidence
                    ? [{ k: 'Evidence', v: Array.isArray(proposal.evidence) ? proposal.evidence.join(' · ') : proposal.evidence, kind: 'prose' as const }]
                    : []),
                ]}
              />
            </div>
          )}

          <Cluster>
            {hasProposal && (
              <>
                <Button variant="primary" busy={busy === 'APPLY'} onClick={() => decide('APPLY')}>
                  Approve
                </Button>
                <Button busy={busy === 'REJECT'} onClick={() => decide('REJECT')}>
                  Reject
                </Button>
              </>
            )}
            <Button busy={busy === 'HOLD'} onClick={() => decide('HOLD')}>
              Hold
            </Button>
            <Button busy={busy === 'IGNORE'} onClick={() => decide('IGNORE')}>
              Ignore
            </Button>
            {!alert.needs_human && (
              <Button busy={busy === 'ack'} onClick={ack}>
                Ack
              </Button>
            )}
          </Cluster>
          {hasProposal && (
            <p className="m-0 text-micro text-ink-faint">
              Approving records your decision and clears the alert. It does not run the fix — no executor consumes
              APPLY yet.
            </p>
          )}
        </Stack>
      </Disclosure>
    </li>
  )
}

/* ------------------------------------------------------------------ review */

function ReviewGroups({ items, onDone, onError }: { items: ReviewItem[]; onDone: (t: string) => void; onError: (t: string) => void }) {
  const run = useAction()
  const [busy, setBusy] = useState<string | null>(null)

  const groups = useMemo(() => {
    const map = new Map<string, ReviewItem[]>()
    for (const it of items) {
      const key = `${it.source ?? 'unknown'} · ${it.kind ?? 'item'}`
      const list = map.get(key) ?? []
      list.push(it)
      map.set(key, list)
    }
    return [...map.entries()].sort((a, b) => b[1].length - a[1].length)
  }, [items])

  async function ackAll(group: ReviewItem[], key: string) {
    setBusy(key)
    for (const it of group) {
      await run(() => ackReview(it.id), { onError: (e) => onError(e.message) })
    }
    setBusy(null)
    onDone(`Acked ${group.length}`)
  }

  return (
    <Stack gap="sm">
      {groups.map(([key, group]) => (
        <Disclosure
          key={key}
          summary={
            <span className="flex min-w-0 flex-wrap items-center gap-2">
              <Chip>{key}</Chip>
              <span className="tnum text-micro text-ink-dim">{group.length}</span>
              <span className="ml-auto shrink-0 text-micro text-ink-faint">
                {relativeTime(group[0]?.created_at)}
              </span>
            </span>
          }
        >
          <Stack gap="sm">
            <Button busy={busy === key} onClick={() => ackAll(group, key)} className="self-start">
              Ack all {group.length}
            </Button>
            <ul className="m-0 list-none p-0">
              {group.slice(0, 25).map((it) => (
                <li key={it.id} className="border-b border-line py-1.5 last:border-b-0">
                  <p className="m-0 font-mono text-micro text-ink-dim">{it.subject}</p>
                  <Text clamp={2}>{it.detail}</Text>
                </li>
              ))}
            </ul>
            {group.length > 25 && (
              <p className="m-0 text-micro text-ink-faint">showing 25 of {group.length}</p>
            )}
          </Stack>
        </Disclosure>
      ))}
    </Stack>
  )
}

/* ------------------------------------------------------------------- page */

export function InboxLanes() {
  const { toasts, push, dismiss } = useToasts()
  const onDone = (t: string) => push('ok', t)
  const onError = (t: string) => push('warn', t)

  const alerts = useResource<AlertsResponse>(K.alerts(undefined, 200), { tier: 'normal' })
  const todos = useResource<TodosResponse>(K.todos, { tier: 'lazy' })
  const proposals = useResource<SoulProposal[]>(K.soulProposals, { tier: 'lazy' })
  const review = useResource<ReviewResponse>(K.review(200), { tier: 'lazy' })
  const run = useAction()
  const [busy, setBusy] = useState<string | null>(null)

  const all = alerts.data?.alerts ?? []
  const needsHuman = all.filter((a) => a.needs_human)
  const fyi = all.filter((a) => !a.needs_human)

  return (
    <>
      <Stack>
        <Card title={`Needs you · ${needsHuman.length}`}>
          <Async r={alerts} skeletonRows={4}>
            {() =>
              needsHuman.length === 0 ? (
                <EmptyState>Nothing is waiting on you.</EmptyState>
              ) : (
                <ul className="m-0 list-none p-0">
                  {needsHuman.map((a) => (
                    <AlertRow key={a.id} alert={a} onDone={onDone} onError={onError} />
                  ))}
                </ul>
              )
            }
          </Async>
        </Card>

        {(proposals.data?.length ?? 0) > 0 && (
          <Card title="Soul proposals">
            <Stack gap="sm">
              {(proposals.data ?? []).map((p) => (
                <div key={p.id} className="rounded-sm border border-line p-2.5">
                  <Cluster>
                    {p.stale && <Chip tone="warn">stale</Chip>}
                    <span className="text-micro text-ink-faint">{relativeTime(p.created_at)}</span>
                  </Cluster>
                  {p.reason && <Text>{p.reason}</Text>}
                  <Cluster className="mt-2">
                    <Button
                      variant="primary"
                      busy={busy === `ap-${p.id}`}
                      onClick={async () => {
                        setBusy(`ap-${p.id}`)
                        // A stale proposal is refused without force; the old UI
                        // sent a plain approve and never showed the refusal.
                        const ok = await run(() => approveProposal(p.id, !!p.stale), {
                          invalidate: ['/dreams'],
                          onError: (e) => onError(e.message),
                        })
                        setBusy(null)
                        if (ok !== undefined) onDone('Proposal approved')
                      }}
                    >
                      {p.stale ? 'Force approve' : 'Approve'}
                    </Button>
                    <Button
                      busy={busy === `rj-${p.id}`}
                      onClick={async () => {
                        setBusy(`rj-${p.id}`)
                        const ok = await run(() => rejectProposal(p.id), {
                          invalidate: ['/dreams'],
                          onError: (e) => onError(e.message),
                        })
                        setBusy(null)
                        if (ok !== undefined) onDone('Proposal rejected')
                      }}
                    >
                      Reject
                    </Button>
                  </Cluster>
                </div>
              ))}
            </Stack>
          </Card>
        )}

        {(todos.data?.tasks?.length ?? 0) > 0 && (
          <Card title={`Proposed tasks · ${todos.data?.tasks.length}`}>
            <ul className="m-0 list-none p-0">
              {(todos.data?.tasks ?? []).map((t: Todo) => (
                <li key={t.id} className="flex flex-wrap items-center gap-2 border-b border-line py-2 last:border-b-0">
                  <span className="min-w-0 flex-1 font-sans text-prose">{t.title || t.content}</span>
                  <Button
                    busy={busy === `ac-${t.id}`}
                    onClick={async () => {
                      setBusy(`ac-${t.id}`)
                      await run(() => acceptTodo(t.id), { invalidate: ['/todos'], onError: (e) => onError(e.message) })
                      setBusy(null)
                    }}
                  >
                    Accept
                  </Button>
                  <Button
                    busy={busy === `dm-${t.id}`}
                    onClick={async () => {
                      setBusy(`dm-${t.id}`)
                      await run(() => dismissTodo(t.id), { invalidate: ['/todos'], onError: (e) => onError(e.message) })
                      setBusy(null)
                    }}
                  >
                    Dismiss
                  </Button>
                </li>
              ))}
            </ul>
          </Card>
        )}

        <Card title={`Review queue · ${review.data?.count ?? review.data?.items?.length ?? 0}`} hint="grouped by source and kind">
          <Async r={review} skeletonRows={3} isEmpty={(d) => (d.items?.length ?? 0) === 0} empty="Review queue is clear.">
            {(d) => <ReviewGroups items={d.items ?? []} onDone={onDone} onError={onError} />}
          </Async>
        </Card>

        {fyi.length > 0 && (
          <Card title={`FYI · ${fyi.length}`} hint="lifecycle events — not waiting on you">
            <Disclosure summary={<span className="text-body text-ink-dim">Show {fyi.length} informational alerts</span>}>
              <ul className="m-0 list-none p-0">
                {fyi.map((a) => (
                  <AlertRow key={a.id} alert={a} onDone={onDone} onError={onError} />
                ))}
              </ul>
            </Disclosure>
          </Card>
        )}

        {alerts.stale && <Notice tone="warn">Showing the last known alerts — the API is not responding.</Notice>}
      </Stack>
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </>
  )
}
