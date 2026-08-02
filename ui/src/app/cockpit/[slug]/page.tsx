'use client'

// ARIA - Coherence C4 cockpit: Per-Project Cockpit.
//
// Multi-panel view of everything about one project: live git state, the
// agents (shells) working it — blocked first — coding sessions with gate
// results, tasks, what changed recently, alerts, Linear items, and spend.

import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'next/navigation'
import { cockpitApi, type ProjectCockpit } from '@/lib/api-client-cockpit'

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
      return 'border-rose-900 bg-rose-950/60 text-rose-300'
    case 'working':
      return 'border-emerald-900 bg-emerald-950/60 text-emerald-300'
    case 'done':
      return 'border-sky-900 bg-sky-950/60 text-sky-300'
    case 'idle':
    default:
      return 'border-stone-800 bg-stone-900 text-stone-400'
  }
}

function Panel({
  title,
  children,
  tone = 'border-stone-800 bg-stone-900',
  className = '',
}: {
  title: string
  children: React.ReactNode
  tone?: string
  className?: string
}) {
  return (
    <section className={`rounded-3xl border p-5 sm:p-6 ${tone} ${className}`}>
      <h2 className="mb-4 text-xs uppercase tracking-[0.3em] text-amber-400">{title}</h2>
      {children}
    </section>
  )
}

function Empty({ text }: { text: string }) {
  return <p className="text-sm text-stone-500">{text}</p>
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
    <main className="min-h-screen bg-stone-950 text-stone-100">
      <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 sm:py-10">
        <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
          <div className="min-w-0">
            <p className="mb-2 text-xs uppercase tracking-[0.3em] text-amber-400">Project Cockpit</p>
            <h1 className="truncate font-serif text-3xl tracking-tight text-stone-50 sm:text-4xl">
              {c?.project?.name || slug}
            </h1>
            {c?.project?.summary && (
              <p className="mt-2 max-w-3xl text-sm text-stone-400">{c.project.summary}</p>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-3">
            {stale && (
              <span
                className="rounded-full border border-amber-900 bg-amber-950/60 px-2.5 py-1 text-[10px] uppercase tracking-wider text-amber-300"
                title="The last refresh failed — showing the previous data."
              >
                stale
              </span>
            )}
            <a href="/cockpit" className="text-sm text-stone-400 hover:text-stone-200">
              ← All projects
            </a>
          </div>
        </header>

        {error && !c && (
          <div className="rounded-3xl border border-rose-900 bg-rose-950/40 p-6 text-sm text-rose-200">
            <p className="mb-1 font-semibold">Cannot load this project&apos;s cockpit.</p>
            <p className="break-words text-rose-300/80">{error}</p>
          </div>
        )}

        {!error && !c && (
          <div className="flex items-center gap-3 text-sm text-stone-400">
            <div className="h-5 w-5 animate-spin rounded-full border-b-2 border-amber-400" />
            Loading cockpit…
          </div>
        )}

        {c && (
          <>
            {/* Budget stat row */}
            <div className="mb-6 grid grid-cols-3 gap-4">
              <div className="rounded-3xl border border-stone-800 bg-stone-900 px-5 py-4">
                <p className="text-[10px] uppercase tracking-[0.25em] text-stone-500">Spend</p>
                <p className="mt-1 font-serif text-2xl text-stone-50">
                  ${(c.budget?.cost ?? 0).toFixed(4)}
                </p>
              </div>
              <div className="rounded-3xl border border-stone-800 bg-stone-900 px-5 py-4">
                <p className="text-[10px] uppercase tracking-[0.25em] text-stone-500">Tokens</p>
                <p className="mt-1 font-serif text-2xl text-stone-50">
                  {(c.budget?.total_tokens ?? 0).toLocaleString()}
                </p>
              </div>
              <div className="rounded-3xl border border-stone-800 bg-stone-900 px-5 py-4">
                <p className="text-[10px] uppercase tracking-[0.25em] text-stone-500">Sessions priced</p>
                <p className="mt-1 font-serif text-2xl text-stone-50">{c.budget?.sessions_priced ?? 0}</p>
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
                        <span className="rounded-full border border-stone-800 bg-stone-950 px-2.5 py-0.5 font-mono text-xs text-stone-200">
                          {c.git.live.branch || 'detached'}
                        </span>
                        {c.git.live.dirty_files > 0 ? (
                          <span className="rounded-full border border-amber-900 bg-amber-950/60 px-2.5 py-0.5 text-xs text-amber-300">
                            {c.git.live.dirty_files} dirty file{c.git.live.dirty_files === 1 ? '' : 's'}
                          </span>
                        ) : (
                          <span className="text-xs text-stone-500">clean</span>
                        )}
                      </div>
                    )}
                    {c.git.harvested && (
                      <div className="text-stone-300">
                        <p className="break-words">{c.git.harvested.last_commit_subject || '—'}</p>
                        <p className="mt-1 text-xs text-stone-500">
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
                      <p className="break-all font-mono text-[11px] text-stone-600">{c.project.path}</p>
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
                      <li key={s.name} className="rounded-2xl border border-stone-800 bg-stone-950 px-4 py-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="truncate font-mono text-sm font-semibold text-stone-100">
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
                            <span className="text-[10px] text-stone-500">@{s.host}</span>
                          )}
                          <span className="ml-auto shrink-0 text-[10px] text-stone-500">
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
                          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded-md border border-rose-900/50 bg-rose-950/30 px-2.5 py-1.5 font-mono text-xs text-rose-200">
                            {s.prompt_line}
                          </pre>
                        )}
                        {s.activity_state !== 'blocked' && s.last_line && (
                          <p className="mt-1.5 truncate font-mono text-xs text-stone-500">{s.last_line}</p>
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
                      <li key={sess.id} className="rounded-2xl border border-stone-800 bg-stone-950 px-4 py-3">
                        <div className="flex flex-wrap items-center gap-2 text-sm">
                          <span className="font-mono text-stone-100">{sess.backend}</span>
                          {sess.model && (
                            <span className="truncate font-mono text-xs text-stone-500">{sess.model}</span>
                          )}
                          <span className="rounded border border-stone-800 bg-stone-900 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-stone-400">
                            {sess.status}
                          </span>
                          {sess.looping && (
                            <span className="rounded border border-amber-900 bg-amber-950/60 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-amber-300">
                              loop
                            </span>
                          )}
                          <span className="ml-auto shrink-0 text-[10px] text-stone-500">
                            {relativeTime(sess.updated_at)}
                          </span>
                        </div>
                        {sess.result_summary && (
                          <p className="mt-1.5 text-xs text-stone-400 line-clamp-2">{sess.result_summary}</p>
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
                                          ? 'border-emerald-900 bg-emerald-950/60 text-emerald-300'
                                          : 'border-rose-900 bg-rose-950/60 text-rose-300'
                                      }`}
                                      title={new Date(g.at).toLocaleString()}
                                    >
                                      {g.passed ? 'pass' : 'fail'}
                                    </summary>
                                    <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md border border-stone-800 bg-black px-2.5 py-2 font-mono text-[11px] text-stone-300">
                                      {g.tail}
                                    </pre>
                                  </details>
                                ) : (
                                  <span
                                    className={`rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider ${
                                      g.passed
                                        ? 'border-emerald-900 bg-emerald-950/60 text-emerald-300'
                                        : 'border-rose-900 bg-rose-950/60 text-rose-300'
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
                        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-stone-600" />
                        <span className="min-w-0 flex-1 break-words text-stone-200">{t.title}</span>
                        {t.stale && (
                          <span className="shrink-0 rounded-full border border-stone-800 bg-stone-900 px-2 py-0.5 text-[10px] uppercase tracking-wider text-stone-500">
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
                      <li key={i} className="font-mono text-xs text-stone-400">
                        <span className="mr-2 text-stone-600">{relativeTime(ch.created_at)}</span>
                        <span className="break-words text-stone-300">{ch.content}</span>
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
                      <li key={i} className="flex items-start gap-2 text-sm text-stone-300">
                        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400/70" />
                        <span className="break-words">{step}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>

              {/* Alerts — only when non-empty */}
              {c.alerts.length > 0 && (
                <Panel title="Alerts" tone="border-rose-900/60 bg-rose-950/20">
                  <ul className="space-y-3">
                    {c.alerts.map((al) => (
                      <li key={al.id} className="text-sm">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="rounded border border-rose-900 bg-rose-950/60 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-rose-300">
                            {al.event_type}
                          </span>
                          <span className="text-[10px] text-stone-500">{al.source}</span>
                          <span className="ml-auto shrink-0 text-[10px] text-stone-500">
                            {relativeTime(al.created_at)}
                          </span>
                        </div>
                        <p className="mt-1 break-words text-rose-100/90">{al.message}</p>
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
                        <span className="min-w-0 flex-1 break-words text-stone-200">{li.title}</span>
                        <span className="shrink-0 rounded border border-stone-800 bg-stone-950 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-stone-400">
                          {li.status}
                        </span>
                        {li.proposed_disposition && (
                          <span className="shrink-0 rounded border border-sky-900 bg-sky-950/60 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-sky-300">
                            {li.proposed_disposition}
                          </span>
                        )}
                        {li.external_ref && (
                          <span className="shrink-0 font-mono text-[10px] text-stone-600">{li.external_ref}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </Panel>
              )}
            </div>

            <p className="mt-6 text-right text-[10px] text-stone-600">
              generated {relativeTime(c.generated_at)}
            </p>
          </>
        )}
      </div>
    </main>
  )
}
