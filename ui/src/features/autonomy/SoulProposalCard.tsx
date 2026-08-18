'use client'

/**
 * ARIA - Soul proposal card
 *
 * Renders one `/dreams/soul-proposals` entry as readable prose. Two measured
 * defects this replaces:
 *  - the old Autonomy page rendered `JSON.stringify(p.proposals)` inside a
 *    `max-h-40 overflow-y-auto` box — a 160px nested scroller that traps the
 *    page scroll on touch and shows escaped markdown instead of the text Ben
 *    is being asked to approve. Current/proposed are wrapped prose blocks now;
 *    the page scrolls, nothing nested does.
 *  - a stale proposal (SOUL.md changed underneath it) is refused by the API
 *    unless `force=true`. The old page always sent a plain approve, the API
 *    refused, and the refusal was swallowed — the button just "did nothing".
 *    Here staleness changes the label to "Force approve" and the refusal, if
 *    any, lands in a toast via onError.
 *
 * Self-contained on purpose: the Inbox carries its own inline copy of this UI
 * (src/features/inbox/InboxLanes.tsx) which should adopt this component in a
 * later pass — that file is another page's scope and is not edited here.
 */
import { useState } from 'react'
import { useAction } from '@/lib/swr'
import { approveProposal, rejectProposal } from '@/lib/api/endpoints'
import type { SoulChange, SoulProposalDetail } from '@/lib/api/types'
import { Chip, Code } from '@/components/ui/primitives'
import { Button } from '@/components/ui/controls'
import { Cluster, Stack } from '@/components/layout'
import { relativeTime } from '@/lib/time'

/** One labelled prose block (CURRENT / PROPOSED / REASON). */
function ProseBlock({ label, children, tone }: { label: string; children: string; tone?: 'accent' }) {
  return (
    <div className={tone === 'accent' ? 'border-l-2 border-accent pl-2.5' : 'border-l-2 border-line pl-2.5'}>
      <p className={`m-0 text-micro uppercase tracking-[0.1em] ${tone === 'accent' ? 'text-accent' : 'text-ink-faint'}`}>
        {label}
      </p>
      {/* whitespace-pre-wrap keeps the proposal's own paragraph breaks; the
          markdown markers it may contain are left as-is — visible text beats a
          renderer that could reflow what Ben is approving. */}
      <p className="m-0 mt-1 max-w-prose whitespace-pre-wrap wrap-anywhere font-sans text-prose leading-relaxed text-ink-dim">
        {children}
      </p>
    </div>
  )
}

function ChangeBlock({ change }: { change: SoulChange }) {
  return (
    <Stack gap="sm">
      {change.section && (
        <p className="m-0 font-sans text-body font-medium text-ink">{change.section}</p>
      )}
      {change.current && <ProseBlock label="Current">{change.current}</ProseBlock>}
      {change.proposed && (
        <ProseBlock label="Proposed" tone="accent">
          {change.proposed}
        </ProseBlock>
      )}
      {change.reason && <ProseBlock label="Reason">{change.reason}</ProseBlock>}
    </Stack>
  )
}

export function SoulProposalCard({
  proposal,
  onDone,
  onError,
}: {
  proposal: SoulProposalDetail
  onDone: (text: string) => void
  onError: (text: string) => void
}) {
  const run = useAction()
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const changes = Array.isArray(proposal.proposals) ? proposal.proposals : []
  const stale = !!proposal.stale

  async function act(kind: 'approve' | 'reject') {
    setBusy(kind)
    const ok = await run(
      // A stale proposal is refused without force — send it, and say so on
      // the button, rather than letting the API refusal look like a no-op.
      () => (kind === 'approve' ? approveProposal(proposal.id, stale) : rejectProposal(proposal.id)),
      { invalidate: ['/dreams'], onError: (e) => onError(e.message) }
    )
    setBusy(null)
    if (ok !== undefined) onDone(kind === 'approve' ? 'Proposal approved' : 'Proposal rejected')
  }

  return (
    <div className="rounded-sm border border-line p-3">
      <Cluster>
        <Chip>
          {changes.length || '?'} change{changes.length === 1 ? '' : 's'}
        </Chip>
        {stale && <Chip tone="warn">stale</Chip>}
        <span className="ml-auto shrink-0 text-micro text-ink-faint">{relativeTime(proposal.created_at)}</span>
      </Cluster>

      <div className="mt-3 flex min-w-0 flex-col gap-4">
        {changes.length > 0 ? (
          changes.map((c, i) => <ChangeBlock key={i} change={c} />)
        ) : (
          // A shape we do not recognise still has to be reviewable — wrapped
          // inline, never inside its own scroller.
          <Code>{JSON.stringify(proposal.proposals ?? proposal, null, 1)}</Code>
        )}
      </div>

      {stale && (
        <p className="m-0 mt-3 font-sans text-micro text-ink-faint">
          SOUL.md changed since this was written
          {proposal.stale_sections?.length ? ` (${proposal.stale_sections.join(', ')})` : ''} — approving
          applies it anyway.
        </p>
      )}

      <Cluster className="mt-3">
        <Button variant="primary" busy={busy === 'approve'} onClick={() => act('approve')}>
          {stale ? 'Force approve' : 'Approve'}
        </Button>
        <Button variant="danger" busy={busy === 'reject'} onClick={() => act('reject')}>
          Reject
        </Button>
      </Cluster>
    </div>
  )
}
