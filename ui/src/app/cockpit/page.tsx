'use client'

// ARIA - Coherence C4 cockpit: Project Switcher.
//
// A card grid of every project, pre-sorted by the API so the one that most
// needs a human is first. Clicking a card opens its per-project cockpit;
// the small "focus" button marks a project active without navigating.

import { useCallback, useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { cockpitApi, type OverviewProject, type ProjectsOverview } from '@/lib/api-client-cockpit'
import { AppShell } from '@/components/AppShell'

function relativeTime(iso: string | null): string {
  if (!iso) return '—'
  const delta = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (delta < 5) return 'just now'
  if (delta < 60) return `${delta}s ago`
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`
  return `${Math.floor(delta / 86400)}d ago`
}

function activityDot(activityStatus: string): string {
  switch (activityStatus) {
    case 'active':
      return 'bg-live shadow-[0_0_8px_rgba(52,211,153,0.6)]'
    case 'idle':
      return 'bg-panel-2'
    default:
      return 'bg-panel-2'
  }
}

function AttentionBadge({ label, count, tone }: { label: string; count: number; tone: string }) {
  if (count === 0) return null
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider ${tone}`}>
      {count} {label}
    </span>
  )
}

function ProjectCard({
  project,
  focused,
  onOpen,
  onFocus,
  focusing,
}: {
  project: OverviewProject
  focused: boolean
  onOpen: () => void
  onFocus: () => void
  focusing: boolean
}) {
  const a = project.attention
  const calm = project.attention_score === 0
  return (
    <div
      onClick={onOpen}
      role="link"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
      className={`group cursor-pointer rounded-3xl border bg-panel p-5 text-left transition hover:border-accent sm:p-6 ${
        focused ? 'border-accent ring-1 ring-accent/60' : 'border-line'
      } ${calm ? 'opacity-80 hover:opacity-100' : ''}`}
    >
      <div className="mb-2 flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className={`h-2 w-2 shrink-0 rounded-full ${activityDot(project.activity_status)}`} />
          <h2 className="truncate font-serif text-xl text-ink">{project.name}</h2>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {focused && (
            <span className="text-[10px] uppercase tracking-[0.2em] text-accent">focused</span>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation()
              onFocus()
            }}
            disabled={focusing || focused}
            title={focused ? 'This project is focused' : 'Focus this project'}
            className="rounded-full border border-line px-2.5 py-0.5 text-[10px] uppercase tracking-wider text-ink-dim transition hover:border-accent hover:text-accent disabled:cursor-default disabled:opacity-40"
          >
            {focusing ? '…' : 'focus'}
          </button>
        </div>
      </div>

      {project.summary && (
        <p className="mb-3 text-sm text-ink-dim line-clamp-2">{project.summary}</p>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        <AttentionBadge
          label="blocked"
          count={a.blocked_shells}
          tone="border-gone bg-gone/60 text-gone"
        />
        <AttentionBadge
          label="gate failed"
          count={a.gate_failed_sessions}
          tone="border-accent bg-accent/60 text-accent"
        />
        <AttentionBadge
          label={a.unacked_alerts === 1 ? 'alert' : 'alerts'}
          count={a.unacked_alerts}
          tone="border-accent bg-accent/60 text-accent"
        />
        <AttentionBadge
          label="stale"
          count={a.stale_tasks}
          tone="border-line bg-panel text-ink-dim"
        />
        <AttentionBadge
          label="working"
          count={a.working_shells + a.running_sessions}
          tone="border-live bg-live/60 text-live"
        />
        {calm && (
          <span className="text-[10px] uppercase tracking-wider text-ink-faint">calm</span>
        )}
      </div>

      <div className="mt-3 flex items-center justify-between gap-2 text-[11px] text-ink-faint">
        <span className="truncate">
          {project.git?.branch ? (
            <span className="font-mono">{project.git.branch}</span>
          ) : (
            project.status
          )}
        </span>
        <span className="shrink-0">{relativeTime(project.last_activity_at)}</span>
      </div>
    </div>
  )
}

export default function CockpitSwitcherPage() {
  const router = useRouter()
  const [overview, setOverview] = useState<ProjectsOverview | null>(null)
  const [stale, setStale] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [focusingSlug, setFocusingSlug] = useState<string | null>(null)
  const hasLoadedRef = useRef(false)

  const refresh = useCallback(async () => {
    // Promise.allSettled-style resilience: a failed poll keeps the last good
    // data on screen and just flips the stale flag — never blank the page.
    const [result] = await Promise.allSettled([cockpitApi.getOverview()])
    if (result.status === 'fulfilled') {
      setOverview(result.value)
      setStale(false)
      setError(null)
      hasLoadedRef.current = true
    } else {
      const message = (result.reason as Error)?.message || 'Failed to load overview'
      if (hasLoadedRef.current) setStale(true)
      else setError(message)
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 10_000)
    return () => clearInterval(id)
  }, [refresh])

  async function handleFocus(slug: string) {
    setFocusingSlug(slug)
    try {
      await cockpitApi.setActive(slug)
      await refresh()
    } catch {
      // A failed focus is non-fatal; the next poll re-syncs the ring.
      setStale(true)
    } finally {
      setFocusingSlug(null)
    }
  }

  return (
    <AppShell area="Supervise">
      <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 sm:py-10">
        <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="mb-2 text-xs uppercase tracking-[0.3em] text-accent">Coherence Cockpit</p>
            <h1 className="font-serif text-3xl tracking-tight text-ink sm:text-4xl">Projects</h1>
            <p className="mt-2 text-sm text-ink-dim">
              Sorted by what needs you most. Pick a project to open its cockpit.
            </p>
          </div>
          <div className="flex items-center gap-3">
            {stale && (
              <span
                className="rounded-full border border-accent bg-accent/60 px-2.5 py-1 text-[10px] uppercase tracking-wider text-accent"
                title="The last refresh failed — showing the previous data."
              >
                stale
              </span>
            )}
            {overview && overview.unacked_alerts_total > 0 && (
              <span className="rounded-full border border-accent bg-accent/60 px-3 py-1 text-xs text-accent">
                {overview.unacked_alerts_total} unacked alert{overview.unacked_alerts_total === 1 ? '' : 's'}
              </span>
            )}
            <a href="/" className="text-sm text-ink-dim hover:text-ink">
              ← Home
            </a>
          </div>
        </header>

        {error && !overview && (
          <div className="rounded-3xl border border-gone bg-gone/40 p-6 text-sm text-gone">
            <p className="mb-1 font-semibold">Cannot load the project overview.</p>
            <p className="break-words text-gone/80">{error}</p>
          </div>
        )}

        {!error && !overview && (
          <div className="flex items-center gap-3 text-sm text-ink-dim">
            <div className="h-5 w-5 animate-spin rounded-full border-b-2 border-accent" />
            Loading projects…
          </div>
        )}

        {overview && overview.projects.length === 0 && (
          <p className="text-sm text-ink-faint">No projects yet — the harvester hasn&apos;t found any.</p>
        )}

        {overview && overview.projects.length > 0 && (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {overview.projects.map((p) => (
              <ProjectCard
                key={p.slug}
                project={p}
                focused={overview.active_project === p.slug}
                focusing={focusingSlug === p.slug}
                onOpen={() => router.push(`/cockpit/${p.slug}`)}
                onFocus={() => handleFocus(p.slug)}
              />
            ))}
          </div>
        )}
      </div>
    </AppShell>
  )
}
