'use client'

/**
 * ARIA - Supervise: the per-project cockpit
 *
 * Replaces /cockpit/[slug]. The two defects that shaped this file:
 *  - The old page ordered its panels git-first and put alerts LAST, so the
 *    things a supervisor exists to catch (a blocked shell, an unacked alert,
 *    a failed gate) sat below the fold. Panels here are ordered by attention:
 *    blocked shells → alerts → failed gates → shells/sessions → git → tasks →
 *    what changed → next steps.
 *  - Budget was a 3-up tile row of serif display numbers squeezed into 326px.
 *    It is one KeyValue table now — numbers, not a hero.
 *
 * Machine strings (paths, last_line, branch, prompt_line) render through
 * Code / KeyValue kind="ident" (break-all), because one unbroken 122-char
 * path is all it takes to widen an implicit grid track past the viewport.
 * Every shell, session and alert row is a real <Link>.
 */
import { ReactNode } from 'react'
import Link from 'next/link'
import { useResource } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type {
  CockpitSession,
  CockpitShell,
  GateRun,
  ProjectCockpit,
  ProjectsOverview,
} from '@/lib/api/types'
import { Card, Chip, Code, EmptyState, KeyValue, Notice, Text } from '@/components/ui/primitives'
import { Async } from '@/components/ui/Async'
import { Disclosure } from '@/components/ui/controls'
import { RetireProject } from './RetireProject'
import { Stack, Cluster, Columns } from '@/components/layout'
import { relativeTime, formatDuration, absoluteTime } from '@/lib/time'
import { usd, count } from '@/lib/format'

const ACTIVITY_TONE: Record<string, 'warn' | 'ok' | 'accent' | 'neutral'> = {
  blocked: 'warn',
  working: 'ok',
  done: 'accent',
  idle: 'neutral',
}

/** Latest gate run decides whether a session belongs in the failures panel. */
function lastGate(s: CockpitSession): GateRun | undefined {
  const runs = s.gate_runs ?? []
  return runs[runs.length - 1]
}

function idleHint(s: CockpitShell): string {
  if (s.idle_seconds == null || s.idle_seconds < 5) return ''
  return `idle ${formatDuration(s.idle_seconds)}`
}

/* -------------------------------------------------------------------- rows */

function ShellRow({ shell }: { shell: CockpitShell }) {
  const blocked = shell.activity_state === 'blocked'
  return (
    <li className="border-b border-line py-1 last:border-b-0">
      <Link
        href={`/supervise/shells/${encodeURIComponent(shell.name)}`}
        className="block min-h-control min-w-0 rounded-sm py-1.5 transition-colors hover:bg-panel-2"
      >
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <Chip tone={ACTIVITY_TONE[shell.activity_state ?? 'idle'] ?? 'neutral'}>{shell.activity_state}</Chip>
          <span className="min-w-0 wrap-anywhere font-mono text-body text-ink">
            {shell.short_name ?? shell.name}
          </span>
          {shell.host && <span className="text-micro text-ink-faint">@{shell.host}</span>}
          <span className="tnum ml-auto shrink-0 text-micro text-ink-faint">{idleHint(shell)}</span>
        </span>
        {blocked && shell.prompt_line ? (
          <Code className="mt-1 block text-gone">{shell.prompt_line}</Code>
        ) : shell.last_line ? (
          <Code className="mt-1 block">{shell.last_line}</Code>
        ) : null}
      </Link>
    </li>
  )
}

function SessionSummary({ session }: { session: CockpitSession }) {
  return (
    <>
      <span className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="font-mono text-body text-ink">{session.backend}</span>
        {session.model && <Code>{session.model}</Code>}
        <Chip>{session.status}</Chip>
        {session.looping && <Chip tone="accent">loop</Chip>}
        {(session.gate_runs ?? []).map((g, i) => (
          <Chip key={i} tone={g.passed ? 'ok' : 'warn'}>
            {g.passed ? 'pass' : 'fail'}
          </Chip>
        ))}
        <span className="ml-auto shrink-0 text-micro text-ink-faint">{relativeTime(session.updated_at)}</span>
      </span>
      {session.result_summary && (
        <Text clamp={2} className="mt-1">
          {session.result_summary}
        </Text>
      )}
    </>
  )
}

/**
 * A session that ran in a shell links to that shell's terminal; one that did
 * not (queued, remote, purged) has nowhere to navigate and stays a plain row.
 */
function SessionRow({ session, children }: { session: CockpitSession; children?: ReactNode }) {
  const body = <SessionSummary session={session} />
  return (
    <li className="border-b border-line py-1 last:border-b-0">
      {session.shell_name ? (
        <Link
          href={`/supervise/shells/${encodeURIComponent(session.shell_name)}`}
          className="block min-h-control min-w-0 rounded-sm py-1.5 transition-colors hover:bg-panel-2"
        >
          {body}
        </Link>
      ) : (
        <div className="min-h-control min-w-0 py-1.5">{body}</div>
      )}
      {children}
    </li>
  )
}

/* -------------------------------------------------------------------- view */

export function ProjectCockpitView({ slug }: { slug: string }) {
  const cockpit = useResource<ProjectCockpit>(K.projectCockpit(slug), { tier: 'fast' })
  // Cache-only read of the overview the list page already holds, so the
  // header paints instantly on navigation; never fetched or polled from here
  // (the detail route's request budget is one key).
  const overview = useResource<ProjectsOverview>(K.projectsOverview, {
    tier: 'static',
    swr: { revalidateOnMount: false, revalidateOnFocus: false, revalidateIfStale: false },
  })
  const seed = overview.data?.projects.find((p) => p.slug === slug)

  return (
    <Async r={cockpit} skeletonRows={8}>
      {(c) => {
        const shells = c.shells ?? []
        const blocked = shells.filter((s) => s.activity_state === 'blocked' || s.awaiting_input)
        const working = shells.filter((s) => !blocked.includes(s))
        const sessions = [...(c.sessions ?? [])].sort((a, b) =>
          (b.updated_at ?? '').localeCompare(a.updated_at ?? '')
        )
        const gateFailed = sessions.filter((s) => lastGate(s)?.passed === false)
        const rest = sessions.filter((s) => !gateFailed.includes(s))
        const alerts = c.alerts ?? []
        const tasks = c.tasks ?? []
        const changed = c.changed ?? []
        const nextSteps = c.project?.next_steps ?? []
        const linear = c.linear ?? []
        const summary = c.project?.summary || seed?.summary || c.project?.charter?.purpose

        return (
          <Stack>
            {summary && <Text clamp={3}>{summary}</Text>}

            {/* Attention first: the panels that exist to interrupt render
                before anything archival, and only when non-empty. */}
            {blocked.length > 0 && (
              <Card title={`Blocked shells · ${blocked.length}`} className="border-gone/50">
                <ul className="m-0 list-none p-0">
                  {blocked.map((s) => (
                    <ShellRow key={s.name} shell={s} />
                  ))}
                </ul>
              </Card>
            )}

            {alerts.length > 0 && (
              <Card title={`Alerts · ${alerts.length}`} hint="unacked, this project" className="border-gone/50">
                <ul className="m-0 list-none p-0">
                  {alerts.map((al) => (
                    <li key={al.id} className="border-b border-line py-1 last:border-b-0">
                      <Link
                        href="/inbox"
                        className="block min-h-control min-w-0 rounded-sm py-1.5 transition-colors hover:bg-panel-2"
                      >
                        <span className="flex min-w-0 flex-wrap items-center gap-2">
                          <Chip tone="warn">{al.event_type || al.source || 'alert'}</Chip>
                          <span className="ml-auto shrink-0 text-micro text-ink-faint">
                            {relativeTime(al.created_at)}
                          </span>
                        </span>
                        <Text clamp={2} className="mt-1">
                          {al.message}
                        </Text>
                      </Link>
                    </li>
                  ))}
                </ul>
              </Card>
            )}

            {gateFailed.length > 0 && (
              <Card title={`Gate failures · ${gateFailed.length}`} className="border-gone/50">
                <ul className="m-0 list-none p-0">
                  {gateFailed.map((s) => {
                    const g = lastGate(s)
                    return (
                      <SessionRow key={s.id} session={s}>
                        {g?.tail && (
                          <pre className="mt-1 max-h-48 overflow-y-auto rounded-sm border border-line bg-panel-2 px-2.5 py-2 font-mono text-micro text-ink-dim">
                            {g.tail}
                          </pre>
                        )}
                        {g?.at && <p className="m-0 mt-1 text-micro text-ink-faint">{absoluteTime(g.at)}</p>}
                      </SessionRow>
                    )
                  })}
                </ul>
              </Card>
            )}

            <Columns lg={2}>
              <Card title={`Agents · ${working.length}`} hint="shells working this project">
                {working.length === 0 ? (
                  <EmptyState>No shells working this project.</EmptyState>
                ) : (
                  <ul className="m-0 list-none p-0">
                    {working.map((s) => (
                      <ShellRow key={s.name} shell={s} />
                    ))}
                  </ul>
                )}
              </Card>

              <Card title={`Sessions · ${rest.length}`}>
                {rest.length === 0 ? (
                  <EmptyState>No coding sessions here.</EmptyState>
                ) : (
                  <ul className="m-0 list-none p-0">
                    {rest.slice(0, 12).map((s) => (
                      <SessionRow key={s.id} session={s} />
                    ))}
                  </ul>
                )}
              </Card>

              <Card title="Git">
                {!c.git?.live && !c.git?.harvested ? (
                  <EmptyState>No git state for this project.</EmptyState>
                ) : (
                  <KeyValue
                    layout="stack"
                    className="mt-0 border-t-0 pt-0"
                    items={[
                      {
                        k: 'Branch',
                        v: c.git?.live?.branch ?? c.git?.harvested?.branch ?? 'detached',
                        kind: 'ident',
                      },
                      ...(c.git?.live
                        ? [
                            {
                              k: 'Working tree',
                              v:
                                (c.git.live.dirty_files ?? 0) > 0
                                  ? `${c.git.live.dirty_files} dirty file${c.git.live.dirty_files === 1 ? '' : 's'}`
                                  : 'clean',
                              kind: 'prose' as const,
                            },
                          ]
                        : []),
                      ...(c.git?.harvested?.last_commit_subject
                        ? [
                            {
                              k: `Last commit · ${relativeTime(c.git.harvested.last_commit_at)}`,
                              v: c.git.harvested.last_commit_subject,
                              kind: 'prose' as const,
                            },
                          ]
                        : []),
                      ...(c.project?.path ? [{ k: 'Path', v: c.project.path, kind: 'ident' as const }] : []),
                      ...(c.vault_folder ? [{ k: 'Vault', v: c.vault_folder, kind: 'ident' as const }] : []),
                    ]}
                  />
                )}
              </Card>

              <Card title={`Tasks · ${tasks.length}`}>
                {tasks.length === 0 ? (
                  <EmptyState>No open tasks.</EmptyState>
                ) : (
                  <ul className="m-0 list-none p-0">
                    {tasks.map((t) => (
                      <li key={t.id} className="flex min-w-0 items-start gap-2 border-b border-line py-2 last:border-b-0">
                        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-ink-mute" aria-hidden="true" />
                        <span className="min-w-0 flex-1 wrap-anywhere font-sans text-prose text-ink">{t.title}</span>
                        {t.stale && <Chip>stale</Chip>}
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card title="What changed" hint="from repo scans and session digests">
                {changed.length === 0 ? (
                  <EmptyState>Nothing captured recently.</EmptyState>
                ) : (
                  <ul className="m-0 list-none p-0">
                    {changed.map((ch, i) => (
                      <li key={i} className="border-b border-line py-1.5 last:border-b-0">
                        <span className="mr-2 text-micro text-ink-faint">{relativeTime(ch.created_at)}</span>
                        <Code>{ch.content}</Code>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card title="Next steps">
                {nextSteps.length === 0 ? (
                  <EmptyState>No next steps recorded.</EmptyState>
                ) : (
                  <ul className="m-0 list-none p-0">
                    {nextSteps.map((step, i) => (
                      <li key={i} className="flex min-w-0 items-start gap-2 border-b border-line py-2 last:border-b-0">
                        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" aria-hidden="true" />
                        <span className="min-w-0 wrap-anywhere font-sans text-prose text-ink-dim">{step}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              {linear.length > 0 && (
                <Card title={`Linear · ${linear.length}`}>
                  <ul className="m-0 list-none p-0">
                    {linear.map((li) => (
                      <li key={li.id} className="flex min-w-0 flex-wrap items-center gap-2 border-b border-line py-2 last:border-b-0">
                        <span className="min-w-0 flex-1 wrap-anywhere font-sans text-prose text-ink">{li.title}</span>
                        <Chip>{li.status}</Chip>
                        {li.proposed_disposition && <Chip tone="accent">{li.proposed_disposition}</Chip>}
                        {li.external_ref && <Code>{li.external_ref}</Code>}
                      </li>
                    ))}
                  </ul>
                </Card>
              )}

              {/* One KeyValue table — the old page burned 3 serif display tiles
                  on numbers that are almost always zero. */}
              <Card title="Budget">
                <KeyValue
                  className="mt-0 border-t-0"
                  items={[
                    { k: 'Spend', v: usd(c.budget?.cost ?? 0), kind: 'num' },
                    { k: 'Tokens', v: count(c.budget?.total_tokens ?? 0), kind: 'num' },
                    { k: 'Sessions priced', v: count(c.budget?.sessions_priced ?? 0), kind: 'num' },
                  ]}
                />
              </Card>
            </Columns>

            {/* Last, behind a disclosure: destructive, and nothing about the
                day-to-day use of this page should put it under a thumb. */}
            <Disclosure summary={<span className="text-body text-ink-dim">Retire this project…</span>}>
              <RetireProject slug={slug} name={c.project?.name || slug} />
            </Disclosure>

            <Cluster justify="justify-between" className="gap-2">
              <span className="text-micro text-ink-faint">generated {relativeTime(c.generated_at)}</span>
              {cockpit.stale && <Chip tone="warn">stale — last refresh failed</Chip>}
            </Cluster>
          </Stack>
        )
      }}
    </Async>
  )
}
