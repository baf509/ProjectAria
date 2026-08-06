'use client'

// ARIA - Coherence C4 cockpit: Per-Project Cockpit.
//
// Multi-panel view of everything about one project: live git state, the
// agents (shells) working it — blocked first — coding sessions with gate
// results, tasks, what changed recently, alerts, Linear items, and spend.

import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'next/navigation'
import { cockpitApi, type ProjectCockpit } from '@/lib/api-client-cockpit'
import { AppShell } from '@/components/AppShell'

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const delta = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (delta < 5) return 'just now'
  if (delta < 60) return `${delta}s ago`
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`
  return `${Math.floor(delta / 86400)}d ago`
}

function activityBadge(state: string): string {
  switch (state) {
    case 'blocked':
      return 'border-gone bg-gone/60 text-gone'
    case 'working':
      return 'border-live bg-live/60 text-live'
    case 'done':
      return 'border-accent bg-accent/10 text-accent'
    case 'idle':
    default:
      return 'border-line bg-panel text-ink-dim'
  }
}

function Panel({
  title,
  children,
  tone = 'border-line bg-panel',
  className = '',
}: {
  title: string
  children: React.ReactNode
  tone?: string
  className?: string
}) {
  return (
    <section className={`rounded-3xl border p-5 sm:p-6 ${tone} ${className}`}>
      <h2 className="mb-4 text-xs uppercase tracking-[0.3em] text-accent">{title}</h2>
      {children}
    </section>
  )
}

function Empty({ text }: { text: string }) {
  return <p className="text-sm text-ink-faint">{text}</p>
}

export default function ProjectCockpitPage() {
  const params = useParams<{ slug: string }>()
  const slug = typeof params?.slug === 'string' ? params.slug : ''
  const [cockpit, setCockpit] = useState<ProjectCockpit | null>(null)
  const [stale, setStale] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const hasLoadedRef = useRef(false)

  const refresh = useCallback(async () => {
    if (!slug) return
    // Keep the last good data on a failed poll — show a stale hint instead of
    // blanking the page.
    const [result] = await Promise.allSettled([cockpitApi.getCockpit(slug)])
    if (result.status === 'fulfilled') {
      setCockpit(result.value)
      setStale(false)
      setError(null)
      hasLoadedRef.current = true
    } else {
      const message = (result.reason as Error)?.message || 'Failed to load cockpit'
      if (hasLoadedRef.current) setStale(true)
      else setError(message)
    }
  }, [slug])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 10_000)
    return () => clearInterval(id)
  }, [refresh])

  const c = cockpit

  return (
    <AppShell area="Supervise">
      <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 sm:py-10">
        <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
          <div className="min-w-0">
            <p className="mb-2 text-xs uppercase tracking-[0.3em] text-accent">Project Cockpit</p>
            <h1 className="truncate font-serif text-3xl tracking-tight text-ink sm:text-4xl">
              {c?.project?.name || slug}
            </h1>
            {c?.project?.summary && (
              <p className="mt-2 max-w-3xl text-sm text-ink-dim">{c.project.summary}</p>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-3">
            {stale && (
              <span
                className="rounded-full border border-accent bg-accent/60 px-2.5 py-1 text-[10px] uppercase tracking-wider text-accent"
                title="The last refresh failed — showing the previous data."
              >
                stale
              </span>
            )}
            <a href="/cockpit" className="text-sm text-ink-dim hover:text-ink">
              ← All projects
            </a>
          </div>
        </header>

        {error && !c && (
          <div className="rounded-3xl border border-gone bg-gone/40 p-6 text-sm text-gone">
            <p className="mb-1 font-semibold">Cannot load this project&apos;s cockpit.</p>
            <p className="break-words text-gone/80">{error}</p>
          </div>
        )}

        {!error && !c && (
          <div className="flex items-center gap-3 text-sm text-ink-dim">
            <div className="h-5 w-5 animate-spin rounded-full border-b-2 border-accent" />
            Loading cockpit…
          </div>
        )}

        {c && (
          <>
            {/* Budget stat row */}
            <div className="mb-6 grid grid-cols-3 gap-4">
              <div className="rounded-3xl border border-line bg-panel px-5 py-4">
                <p className="text-[10px] uppercase tracking-[0.25em] text-ink-faint">Spend</p>
                <p className="mt-1 font-serif text-2xl text-ink">
                  ${(c.budget?.cost ?? 0).toFixed(4)}
                </p>
              </div>
              <div className="rounded-3xl border border-line bg-panel px-5 py-4">
                <p className="text-[10px] uppercase tracking-[0.25em] text-ink-faint">Tokens</p>
                <p className="mt-1 font-serif text-2xl text-ink">
                  {(c.budget?.total_tokens ?? 0).toLocaleString()}
                </p>
              </div>
              <div className="rounded-3xl border border-line bg-panel px-5 py-4">
                <p className="text-[10px] uppercase tracking-[0.25em] text-ink-faint">Sessions priced</p>
                <p className="mt-1 font-serif text-2xl text-ink">{c.budget?.sessions_priced ?? 0}</p>
              </div>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              {/* Git */}
              <Panel title="Git">
                {!c.git?.live && !c.git?.harvested ? (
                  <Empty text="No git state for this project." />
                ) : (
                  <div className="space-y-3 text-sm">
                    {c.git.live && (
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="rounded-full border border-line bg-ground px-2.5 py-0.5 font-mono text-xs text-ink">
                          {c.git.live.branch || 'detached'}
                        </span>
                        {c.git.live.dirty_files > 0 ? (
                          <span className="rounded-full border border-accent bg-accent/60 px-2.5 py-0.5 text-xs text-accent">
                            {c.git.live.dirty_files} dirty file{c.git.live.dirty_files === 1 ? '' : 's'}
                          </span>
                        ) : (
                          <span className="text-xs text-ink-faint">clean</span>
                        )}
                      </div>
                    )}
                    {c.git.harvested && (
                      <div className="text-ink-dim">
                        <p className="break-words">{c.git.harvested.last_commit_subject || '—'}</p>
                        <p className="mt-1 text-xs text-ink-faint">
                          last commit {relativeTime(c.git.harvested.last_commit_at)}
                          {c.git.harvested.branch && !c.git.live ? (
                            <>
                              {' '}on <span className="font-mono">{c.git.harvested.branch}</span>
                            </>
                          ) : null}
                        </p>
                      </div>
                    )}
                    {c.project?.path && (
                      <p className="break-all font-mono text-[11px] text-ink-faint">{c.project.path}</p>
                    )}
                  </div>
                )}
              </Panel>

              {/* Agents (shells) */}
              <Panel title="Agents">
                {c.shells.length === 0 ? (
                  <Empty text="No shells working this project." />
                ) : (
                  <ul className="space-y-3">
                    {c.shells.map((s) => (
                      <li key={s.name} className="rounded-2xl border border-line bg-ground px-4 py-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="truncate font-mono text-sm font-semibold text-ink">
                            {s.short_name}
                          </span>
                          <span
                            className={`rounded border px-1.5 py-0.5 text-[9px] uppercase tracking-wider ${activityBadge(
                              s.activity_state
                            )}`}
                          >
                            {s.activity_state}
                          </span>
                          {s.host && (
                            <span className="text-[10px] text-ink-faint">@{s.host}</span>
                          )}
                          <span className="ml-auto shrink-0 text-[10px] text-ink-faint">
                            {s.idle_seconds != null && s.idle_seconds >= 5
                              ? `idle ${
                                  s.idle_seconds < 60
                                    ? `${Math.floor(s.idle_seconds)}s`
                                    : s.idle_seconds < 3600
                                    ? `${Math.floor(s.idle_seconds / 60)}m`
                                    : `${Math.floor(s.idle_seconds / 3600)}h`
                                }`
                              : ''}
                          </span>
                        </div>
                        {s.activity_state === 'blocked' && s.prompt_line && (
                          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded-md border border-gone/50 bg-gone/30 px-2.5 py-1.5 font-mono text-xs text-gone">
                            {s.prompt_line}
                          </pre>
                        )}
                        {s.activity_state !== 'blocked' && s.last_line && (
                          <p className="mt-1.5 truncate font-mono text-xs text-ink-faint">{s.last_line}</p>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>

              {/* Sessions */}
              <Panel title="Sessions">
                {c.sessions.length === 0 ? (
                  <Empty text="No coding sessions here." />
                ) : (
                  <ul className="space-y-3">
                    {c.sessions.map((sess) => (
                      <li key={sess.id} className="rounded-2xl border border-line bg-ground px-4 py-3">
                        <div className="flex flex-wrap items-center gap-2 text-sm">
                          <span className="font-mono text-ink">{sess.backend}</span>
                          {sess.model && (
                            <span className="truncate font-mono text-xs text-ink-faint">{sess.model}</span>
                          )}
                          <span className="rounded border border-line bg-panel px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-ink-dim">
                            {sess.status}
                          </span>
                          {sess.looping && (
                            <span className="rounded border border-accent bg-accent/60 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-accent">
                              loop
                            </span>
                          )}
                          <span className="ml-auto shrink-0 text-[10px] text-ink-faint">
                            {relativeTime(sess.updated_at)}
                          </span>
                        </div>
                        {sess.result_summary && (
                          <p className="mt-1.5 text-xs text-ink-dim line-clamp-2">{sess.result_summary}</p>
                        )}
                        {sess.gate_runs && sess.gate_runs.length > 0 && (
                          <div className="mt-2 flex flex-wrap items-center gap-1.5">
                            {sess.gate_runs.map((g, i) => (
                              <span key={i} className="inline-flex">
                                {g.tail ? (
                                  <details className="group">
                                    <summary
                                      className={`cursor-pointer list-none rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider ${
                                        g.passed
                                          ? 'border-live bg-live/60 text-live'
                                          : 'border-gone bg-gone/60 text-gone'
                                      }`}
                                      title={new Date(g.at).toLocaleString()}
                                    >
                                      {g.passed ? 'pass' : 'fail'}
                                    </summary>
                                    <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md border border-line bg-ground px-2.5 py-2 font-mono text-[11px] text-ink-dim">
                                      {g.tail}
                                    </pre>
                                  </details>
                                ) : (
                                  <span
                                    className={`rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider ${
                                      g.passed
                                        ? 'border-live bg-live/60 text-live'
                                        : 'border-gone bg-gone/60 text-gone'
                                    }`}
                                    title={new Date(g.at).toLocaleString()}
                                  >
                                    {g.passed ? 'pass' : 'fail'}
                                  </span>
                                )}
                              </span>
                            ))}
                          </div>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>

              {/* Tasks */}
              <Panel title="Tasks">
                {c.tasks.length === 0 ? (
                  <Empty text="No open tasks." />
                ) : (
                  <ul className="space-y-2">
                    {c.tasks.map((t) => (
                      <li key={t.id} className="flex items-start gap-2 text-sm">
                        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-panel-2" />
                        <span className="min-w-0 flex-1 break-words text-ink">{t.title}</span>
                        {t.stale && (
                          <span className="shrink-0 rounded-full border border-line bg-panel px-2 py-0.5 text-[10px] uppercase tracking-wider text-ink-faint">
                            stale
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>

              {/* What changed */}
              <Panel title="What Changed">
                {c.changed.length === 0 ? (
                  <Empty text="Nothing captured recently." />
                ) : (
                  <ul className="space-y-2">
                    {c.changed.map((ch, i) => (
                      <li key={i} className="font-mono text-xs text-ink-dim">
                        <span className="mr-2 text-ink-faint">{relativeTime(ch.created_at)}</span>
                        <span className="break-words text-ink-dim">{ch.content}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>

              {/* Next steps (from the project doc, when present) */}
              <Panel title="Next Steps">
                {!c.project?.next_steps || c.project.next_steps.length === 0 ? (
                  <Empty text="No next steps recorded." />
                ) : (
                  <ul className="space-y-2">
                    {c.project.next_steps.map((step, i) => (
                      <li key={i} className="flex items-start gap-2 text-sm text-ink-dim">
                        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent/70" />
                        <span className="break-words">{step}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>

              {/* Alerts — only when non-empty */}
              {c.alerts.length > 0 && (
                <Panel title="Alerts" tone="border-gone/60 bg-gone/20">
                  <ul className="space-y-3">
                    {c.alerts.map((al) => (
                      <li key={al.id} className="text-sm">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="rounded border border-gone bg-gone/60 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-gone">
                            {al.event_type}
                          </span>
                          <span className="text-[10px] text-ink-faint">{al.source}</span>
                          <span className="ml-auto shrink-0 text-[10px] text-ink-faint">
                            {relativeTime(al.created_at)}
                          </span>
                        </div>
                        <p className="mt-1 break-words text-gone">{al.message}</p>
                      </li>
                    ))}
                  </ul>
                </Panel>
              )}

              {/* Linear — only when non-empty */}
              {c.linear.length > 0 && (
                <Panel title="Linear">
                  <ul className="space-y-2">
                    {c.linear.map((li) => (
                      <li key={li.id} className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="min-w-0 flex-1 break-words text-ink">{li.title}</span>
                        <span className="shrink-0 rounded border border-line bg-ground px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-ink-dim">
                          {li.status}
                        </span>
                        {li.proposed_disposition && (
                          <span className="shrink-0 rounded border border-accent bg-accent/10 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-accent">
                            {li.proposed_disposition}
                          </span>
                        )}
                        {li.external_ref && (
                          <span className="shrink-0 font-mono text-[10px] text-ink-faint">{li.external_ref}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </Panel>
              )}
            </div>

            <p className="mt-6 text-right text-[10px] text-ink-faint">
              generated {relativeTime(c.generated_at)}
            </p>
          </>
        )}
      </div>
    </AppShell>
  )
}
