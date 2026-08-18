'use client'

/**
 * ARIA - Supervise: the project switcher
 *
 * Replaces /cockpit, whose card grid had no base column — one unbreakable
 * token made every implicit track 450px wide in a 390px viewport (+92/107px,
 * the measured worst overflow in the app). Cards now live in <Grid min="18rem">
 * whose tracks are minmax(min(100%,18rem),1fr), so no string can widen them.
 *
 * Structure choices, each fixing a measured defect:
 *  - A card is a real <Link>, not a div[role=link] wrapping a <button>
 *    (invalid ARIA, no prefetch, no long-press menu).
 *  - 63 of 64 rows are calm inventory (scratch dirs, worktrees, /tmp paths).
 *    They render as compact single-line rows behind a Disclosure instead of
 *    63 full cards — hierarchy, not the old `opacity-80`, which multiplied
 *    already-failing ink-faint contrast (2.9:1) further down.
 *  - `kind` is not on /projects/overview yet (§4.7 companion API item), so
 *    "matters" is kind === 'project' or a non-zero attention score.
 */
import { useMemo, useState } from 'react'
import Link from 'next/link'
import {} from 'lucide-react'
import { useResource, useAction } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type { FleetOverview, FleetShell, OverviewProject, ProjectsOverview } from '@/lib/api/types'
import { Card, Chip, Code, EmptyState, Notice, Text } from '@/components/ui/primitives'
import { Button, Disclosure, Input, Toasts, type Toast } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Stack, Cluster, Grid, Row } from '@/components/layout'
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

/* ------------------------------------------------------------- attention -- */

const ACTIVITY_DOT: Record<string, string> = {
  active: 'bg-live',
  working: 'bg-live',
  blocked: 'bg-gone',
  idle: 'bg-idle',
}

function ActivityDot({ state }: { state?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-block h-[7px] w-[7px] shrink-0 rounded-full ${ACTIVITY_DOT[state ?? ''] ?? 'bg-ink-mute'}`}
    />
  )
}

/** The attention chips: blocked/gate/alerts warn, working ok, stale neutral. */
function AttentionChips({ p }: { p: OverviewProject }) {
  const a = p.attention ?? {}
  const working = (a.working_shells ?? 0) + (a.running_sessions ?? 0)
  const chips: Array<{ label: string; count: number; tone: 'warn' | 'accent' | 'ok' | 'neutral' }> = [
    { label: 'blocked', count: a.blocked_shells ?? 0, tone: 'warn' },
    { label: 'gate failed', count: a.gate_failed_sessions ?? 0, tone: 'warn' },
    { label: (a.unacked_alerts ?? 0) === 1 ? 'alert' : 'alerts', count: a.unacked_alerts ?? 0, tone: 'accent' },
    { label: 'stale', count: a.stale_tasks ?? 0, tone: 'neutral' },
    { label: 'working', count: working, tone: 'ok' },
  ]
  const visible = chips.filter((c) => c.count > 0)
  if (visible.length === 0) return <Chip>calm</Chip>
  return (
    <>
      {visible.map((c) => (
        <Chip key={c.label} tone={c.tone}>
          {c.count} {c.label}
        </Chip>
      ))}
    </>
  )
}

/* ------------------------------------------------------------------ cards -- */

function ProjectCard({
  p,

}: {
  p: OverviewProject

}) {
  return (
    <div className="relative min-w-0">
      <Link
        href={`/supervise/projects/${encodeURIComponent(p.slug)}`}
        className="block min-w-0 rounded border border-line bg-panel p-3.5 transition-colors hover:border-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent"
      >
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <ActivityDot state={p.activity_status} />
          {/* The name WRAPS (wrap-anywhere): truncating it was how the old grid
              hid that its tracks were wider than the viewport. */}
          <span className="min-w-0 wrap-anywhere text-title text-ink">{p.name}</span>
        </span>
        {p.summary && (
          <Text clamp={2} className="mt-1.5">
            {p.summary}
          </Text>
        )}
        <span className="mt-2.5 flex min-w-0 flex-wrap items-center gap-1.5">
          <AttentionChips p={p} />
        </span>
        <span className="mt-2.5 flex min-w-0 items-center justify-between gap-2">
          <Code className="min-w-0">{p.git?.branch ?? p.status ?? ''}</Code>
          <span className="tnum shrink-0 text-micro text-ink-faint">{relativeTime(p.last_activity_at)}</span>
        </span>
      </Link>
    </div>
  )
}

/* -------------------------------------------------------------- fleet strip */

const SHELL_ORDER: Record<string, number> = { blocked: 0, working: 2, done: 3, idle: 4 }

function FleetStrip({ shells }: { shells: FleetShell[] }) {
  const sorted = [...shells].sort(
    (a, b) =>
      (a.awaiting_input ? 1 : SHELL_ORDER[a.activity_state ?? 'idle'] ?? 5) -
      (b.awaiting_input ? 1 : SHELL_ORDER[b.activity_state ?? 'idle'] ?? 5)
  )
  return (
    <Cluster nowrap className="gap-2 py-0.5">
      {sorted.map((s) => (
        <Link
          key={s.name}
          href={`/supervise/shells/${encodeURIComponent(s.name)}`}
          className={`flex min-h-control shrink-0 snap-start items-center gap-1.5 rounded-sm border px-2.5 transition-colors hover:border-accent ${
            s.activity_state === 'blocked' || s.awaiting_input ? 'border-gone/50 bg-gone/10' : 'border-line bg-panel-2'
          }`}
        >
          <ActivityDot state={s.activity_state} />
          <span className="whitespace-nowrap font-mono text-label text-ink">{s.short_name ?? s.name}</span>
          {(s.awaiting_input || s.activity_state === 'blocked') && <Chip tone="warn">input?</Chip>}
        </Link>
      ))}
    </Cluster>
  )
}

/* ------------------------------------------------------------------- board */

export function ProjectsBoard() {
  const { toasts, push, dismiss } = useToasts()
  const overview = useResource<ProjectsOverview>(K.projectsOverview, { tier: 'slow' })
  const fleet = useResource<FleetOverview>(K.shellsOverview, { tier: 'fast' })
  const run = useAction()
  const [query, setQuery] = useState('')
  const [attentionOnly, setAttentionOnly] = useState(false)

  const { cards, inventory } = useMemo(() => {
    const q = query.trim().toLowerCase()
    const all = (overview.data?.projects ?? []).filter(
      (p) =>
        !q ||
        p.name.toLowerCase().includes(q) ||
        p.slug.toLowerCase().includes(q) ||
        (p.path ?? '').toLowerCase().includes(q)
    )
    const matters = (p: OverviewProject) =>
      p.kind === 'project' || (p.attention_score ?? 0) > 0
    const withAttention = (p: OverviewProject) => (p.attention_score ?? 0) > 0
    const shown = attentionOnly ? all.filter(withAttention) : all
    return {
      cards: shown.filter(matters),
      inventory: shown.filter((p) => !matters(p)),
    }
  }, [overview.data, query, attentionOnly])


  return (
    <>
      <Stack>
        <Card title={`Fleet · ${fleet.data?.shells?.length ?? 0}`} hint="watched shells">
          <Async
            r={fleet}
            skeletonRows={1}
            isEmpty={(d) => (d.shells?.length ?? 0) === 0}
            empty="No watched shells right now."
          >
            {(d) => <FleetStrip shells={d.shells ?? []} />}
          </Async>
        </Card>

        <Cluster className="gap-2">
          <Input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter projects…"
            aria-label="Filter projects"
            // coarse:text-title (1rem) outranks fieldBase's text-body (14px on
            // touch), which would re-trigger iOS focus-zoom — the shared
            // controls.tsx fix is noted upstream; this is the local guard.
            className="max-w-xs flex-1 coarse:text-title"
          />
          <Button
            aria-pressed={attentionOnly}
            onClick={() => setAttentionOnly((v) => !v)}
            className={attentionOnly ? 'border-accent bg-accent/10 text-accent' : undefined}
          >
            Attention only
          </Button>
        </Cluster>

        <Async r={overview} skeletonRows={5}>
          {() => (
            <>
              {cards.length === 0 ? (
                <EmptyState>
                  {attentionOnly || query
                    ? 'No projects match.'
                    : 'Nothing needs attention — every project is calm.'}
                </EmptyState>
              ) : (
                <Grid min="18rem">
                  {cards.map((p) => (
                    <ProjectCard
                      key={p.slug}
                      p={p}
                    />
                  ))}
                </Grid>
              )}

              {inventory.length > 0 && (
                <Card>
                  <Disclosure
                    summary={
                      <span className="text-body text-ink-dim">
                        Inventory · <span className="tnum">{inventory.length}</span>{' '}
                        <span className="text-micro text-ink-faint">calm — scratch dirs, worktrees, side repos</span>
                      </span>
                    }
                  >
                    <ul className="m-0 list-none p-0">
                      {inventory.map((p) => (
                        <li key={p.slug} className="border-b border-line last:border-b-0">
                          <Row
                            as={Link}
                            href={`/supervise/projects/${encodeURIComponent(p.slug)}`}
                            marker={<ActivityDot state={p.activity_status} />}
                            trailing={relativeTime(p.last_activity_at)}
                            className="transition-colors hover:bg-panel-2"
                          >
                            <span className="block min-w-0 wrap-anywhere font-mono text-body text-ink">{p.name}</span>
                          </Row>
                        </li>
                      ))}
                    </ul>
                  </Disclosure>
                </Card>
              )}
            </>
          )}
        </Async>

        {overview.stale && (
          <Notice tone="warn">Showing the last known projects — the API is not responding.</Notice>
        )}
      </Stack>
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </>
  )
}
