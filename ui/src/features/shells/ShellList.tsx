'use client'

/**
 * ARIA - watched-shell fleet list
 *
 * The old page fetched the UNFILTERED `/shells` (232KB, 661 shells, 659 of
 * them stopped) every 10s and rendered every one as a row — 6,046 DOM nodes on
 * a phone. This list is built from what the fleet actually is:
 *  - the live set (`/shells?status=active,idle`, 778B) merged with the
 *    overview digest (`awaiting_input` / `activity_state` / `last_line`),
 *    attention first;
 *  - stopped shells load ON DEMAND behind an expander and render 50 at a
 *    time, so the 661-shell fixture stays under the DOM budget;
 *  - rows are memoised Links (`cv-auto`), so a poll that changes nothing
 *    re-renders nothing, and selection is ROUTING — it survives reload and
 *    Back, which `useState(selected)` never did.
 */
import { memo, useMemo, useState } from 'react'
import Link from 'next/link'
import { useSelectedLayoutSegment } from 'next/navigation'
import { useResource } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type { FleetOverviewPayload, FleetShell, ShellListPayload } from '@/lib/api/types'
import { Chip, EmptyState, Notice, Skeleton } from '@/components/ui/primitives'
import { Button, Input, TabStrip, Toasts } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Cluster } from '@/components/layout'
import { relativeTime } from '@/lib/time'
import { NewSessionSheet } from './NewSessionSheet'
import { useToasts } from './useToasts'

type Lane = 'all' | 'attention' | 'working' | 'idle'

/** blocked/awaiting → 0, working → 1, everything else → 2. */
function laneRank(s: FleetShell): number {
  if (s.awaiting_input || s.activity_state === 'blocked') return 0
  if (s.activity_state === 'working') return 1
  return 2
}

function matchesFilter(s: FleetShell, q: string): boolean {
  if (!q) return true
  return (
    (s.short_name ?? '').toLowerCase().includes(q) ||
    s.name.toLowerCase().includes(q) ||
    (s.project_dir ?? '').toLowerCase().includes(q) ||
    (s.tags ?? []).some((t) => t.toLowerCase().includes(q))
  )
}

const DOT: Record<string, string> = {
  working: 'bg-live',
  blocked: 'bg-gone',
  done: 'bg-accent',
  idle: 'bg-idle',
  stopped: 'bg-ink-mute',
}

const ShellRow = memo(function ShellRow({
  shell,
  selected,
  showHost,
}: {
  shell: FleetShell
  selected: boolean
  showHost: boolean
}) {
  const state = shell.status === 'stopped' ? 'stopped' : (shell.activity_state ?? 'idle')
  const preview = shell.last_line?.trim() || shell.project_dir || ''
  return (
    <li className="cv-auto border-b border-line last:border-b-0">
      <Link
        href={`/supervise/shells/${encodeURIComponent(shell.name)}`}
        aria-current={selected ? 'page' : undefined}
        className={`grid min-h-row grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-2.5 px-2.5 py-1.5 transition-colors hover:bg-panel-2 ${
          selected ? 'border-l-2 border-l-accent bg-panel-2 pl-2' : ''
        }`}
      >
        <span
          aria-hidden="true"
          className={`inline-block h-[7px] w-[7px] shrink-0 rounded-full ${DOT[state] ?? 'bg-ink-mute'}`}
        />
        <span className="min-w-0">
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="min-w-0 break-all font-mono text-body text-ink">
              {shell.short_name ?? shell.name}
            </span>
            {(shell.awaiting_input || shell.activity_state === 'blocked') && (
              <Chip tone="warn">awaiting</Chip>
            )}
            {shell.activity_state === 'done' && <Chip tone="accent">done</Chip>}
            {showHost && shell.host && <Chip>{shell.host}</Chip>}
          </span>
          {preview && (
            // One-line preview, not the record of truth — the full scrollback
            // is one tap away on the detail, so a clamp is legitimate here.
            <span className="mt-0.5 line-clamp-1 wrap-anywhere font-mono text-micro text-ink-faint">
              {preview}
            </span>
          )}
        </span>
        <span className="tnum shrink-0 whitespace-nowrap text-micro text-ink-faint">
          {relativeTime(shell.last_activity_at)}
        </span>
      </Link>
    </li>
  )
})

/**
 * Mounted only after the expander opens, so the 659-row payload is never
 * fetched on page load (`tier: 'static'` — a stopped shell does not change).
 */
function StoppedShells({ filter, selected }: { filter: string; selected: string | null }) {
  const stopped = useResource<ShellListPayload>(K.shells('stopped'), { tier: 'static' })
  const [pages, setPages] = useState(1)
  const PAGE = 50

  const rows = useMemo(() => {
    const list = (stopped.data?.shells ?? []).filter((s) => matchesFilter(s, filter))
    list.sort(
      (a, b) => new Date(b.last_activity_at ?? 0).getTime() - new Date(a.last_activity_at ?? 0).getTime()
    )
    return list
  }, [stopped.data, filter])

  const shown = rows.slice(0, pages * PAGE)
  return (
    <Async r={stopped} skeletonRows={3} isEmpty={(d) => d.shells.length === 0} empty="No stopped shells.">
      {() => (
        <>
          <ul className="m-0 list-none p-0">
            {shown.map((s) => (
              <ShellRow key={s.name} shell={s} selected={s.name === selected} showHost={false} />
            ))}
          </ul>
          {rows.length > shown.length && (
            <div className="px-2.5 py-2">
              <Button onClick={() => setPages((p) => p + 1)} className="w-full">
                Show {Math.min(PAGE, rows.length - shown.length)} more · {rows.length - shown.length} left
              </Button>
            </div>
          )}
        </>
      )}
    </Async>
  )
}

export function ShellList() {
  const segment = useSelectedLayoutSegment()
  const selected = segment ? decodeURIComponent(segment) : null
  const { toasts, push, dismiss } = useToasts()

  const live = useResource<ShellListPayload>(K.shells('active,idle'), { tier: 'fast' })
  const overview = useResource<FleetOverviewPayload>(K.shellsOverview, { tier: 'fast' })

  const [filterText, setFilterText] = useState('')
  const [lane, setLane] = useState<Lane>('all')
  const [stoppedOpen, setStoppedOpen] = useState(false)
  const [newOpen, setNewOpen] = useState(false)

  // The live list is the authority on membership; the overview enriches it
  // (awaiting/blocked/last_line come from a server-side tmux tail).
  const rows = useMemo<FleetShell[]>(() => {
    const enrich = new Map((overview.data?.shells ?? []).map((s) => [s.name, s]))
    const base: FleetShell[] = live.data?.shells
      ? live.data.shells.map((s) => ({ ...s, ...(enrich.get(s.name) ?? {}) }))
      : overview.data?.shells ?? []
    const q = filterText.trim().toLowerCase()
    const out = base.filter((s) => matchesFilter(s, q))
    out.sort((a, b) => {
      const lane = laneRank(a) - laneRank(b)
      if (lane !== 0) return lane
      return new Date(b.last_activity_at ?? 0).getTime() - new Date(a.last_activity_at ?? 0).getTime()
    })
    return out
  }, [live.data, overview.data, filterText])

  const counts = useMemo(() => {
    const c = { all: rows.length, attention: 0, working: 0, idle: 0 }
    for (const s of rows) {
      const r = laneRank(s)
      if (r === 0) c.attention++
      else if (r === 1) c.working++
      else c.idle++
    }
    return c
  }, [rows])

  const visible = lane === 'all' ? rows : rows.filter((s) => laneRank(s) === { attention: 0, working: 1, idle: 2 }[lane])
  const hosts = new Set(rows.map((s) => s.host).filter(Boolean))
  const filterQ = filterText.trim().toLowerCase()

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 border-b border-line px-2.5 py-2">
        <Cluster gap="gap-2" className="flex-nowrap">
          <Input
            value={filterText}
            onChange={(e) => setFilterText(e.target.value)}
            placeholder="Filter by name, project, tag"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            className="flex-1 coarse:[font-size:1rem]"
          />
          <Button onClick={() => setNewOpen(true)} className="shrink-0">
            New
          </Button>
        </Cluster>
        <div className="mt-2">
          <TabStrip
            items={[
              { key: 'all', label: 'All', count: counts.all },
              { key: 'attention', label: 'Awaiting', count: counts.attention },
              { key: 'working', label: 'Working', count: counts.working },
              { key: 'idle', label: 'Idle', count: counts.idle },
            ]}
            active={lane}
            onSelect={(k) => setLane(k as Lane)}
          />
        </div>
      </div>

      {/* The flush shell gives no tab-bar clearance (main is full-bleed), so
          this scroller pads its own bottom — without it the stopped-shells
          expander and the "Show more" pager sat behind the fixed BottomTabs
          and could not be tapped (measured: playwright tap intercepted by
          nav). The nav is lg:hidden, so the padding is too. */}
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain pb-[calc(var(--tabbar-h)+var(--sab))] lg:pb-0">
        {/* Two sources back one list, so <Async> (which wraps ONE resource)
            does not fit: either payload is enough to render, and an error is
            terminal only when BOTH are empty-handed. */}
        {live.data === undefined && overview.data === undefined ? (
          live.error && overview.error ? (
            <Notice tone="warn" className="m-2.5">
              <span className="wrap-anywhere">{live.error.message}</span>
            </Notice>
          ) : (
            <Skeleton rows={6} className="p-2.5" />
          )
        ) : visible.length === 0 ? (
          <div className="p-4">
            <EmptyState>
              {rows.length === 0
                ? 'No live shells. Start one with `aria shells new` or the New button.'
                : 'No shells match this filter.'}
            </EmptyState>
          </div>
        ) : (
          <ul className="m-0 list-none p-0">
            {visible.map((s) => (
              <ShellRow key={s.name} shell={s} selected={s.name === selected} showHost={hosts.size > 1} />
            ))}
          </ul>
        )}

        {/* Fetch-on-open: <details> mounts its children even when closed, so a
            Disclosure here would fetch the 659-row payload on page load. */}
        <div className="border-t border-line">
          <Button
            variant="ghost"
            onClick={() => setStoppedOpen((o) => !o)}
            aria-expanded={stoppedOpen}
            className="w-full justify-start"
          >
            <span aria-hidden="true" className={`transition-transform ${stoppedOpen ? 'rotate-90' : ''}`}>
              ▸
            </span>
            Stopped shells
          </Button>
          {stoppedOpen && <StoppedShells filter={filterQ} selected={selected} />}
        </div>

        {(live.stale || overview.stale) && (
          <Notice tone="warn" className="m-2.5">
            Showing the last known fleet — the API is not responding.
          </Notice>
        )}
      </div>

      <NewSessionSheet
        open={newOpen}
        onClose={() => setNewOpen(false)}
        onDone={(t) => push('ok', t)}
        onError={(t) => push('warn', t)}
      />
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </div>
  )
}
