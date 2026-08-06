/**
 * Persistent application shell.
 *
 * The five areas are the redesign's information architecture, grouped by the
 * posture you're in rather than by feature category — which is how the old
 * dashboard ended up with Memory Browser next to Cutover Readiness. Nav is
 * always present so switching areas never bounces you off a launcher screen.
 *
 * Areas marked `soon` are routes that don't exist yet; they render as disabled
 * rather than being hidden, so the shape of the app is legible from day one
 * and a dead link is impossible.
 */
'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { ReactNode } from 'react'

type Area = { href: string; label: string; blurb: string; soon?: boolean }

const AREAS: Area[] = [
  { href: '/inbox', label: 'Inbox', blurb: 'waiting on you' },
  { href: '/chat', label: 'Converse', blurb: 'chat, voice, history' },
  { href: '/cockpit', label: 'Supervise', blurb: 'sessions, shells, agents' },
  { href: '/operate', label: 'Operate', blurb: 'models, benchmarks, health' },
  { href: '/dashboard', label: 'Know', blurb: 'memory, research, projects' },
  { href: '/autonomy', label: 'Autonomy', blurb: 'awareness, dreams' },
]

export function AppShell({
  area,
  status,
  children,
  flush = false,
}: {
  area: string
  status?: ReactNode
  children: ReactNode
  /** Full-bleed surfaces (chat) manage their own height and padding. */
  flush?: boolean
}) {
  const pathname = usePathname()

  // Flush surfaces (chat) must fit the viewport exactly, so the shell becomes a
  // fixed-height flex column and the child fills the remainder. Using flex
  // rather than subtracting a hardcoded chrome height keeps it correct on
  // mobile too, where the nav stacks above the content instead of beside it.
  return (
    <div
      className={`bg-ground font-mono text-[13px] text-ink ${
        flush ? 'h-[100dvh] overflow-hidden' : 'min-h-screen'
      }`}
    >
      {/* Nav is horizontal and scrollable on narrow screens, a rail from lg up.
          It never disappears — that is the point of the shell. */}
      <div className={`lg:flex ${flush ? 'h-full min-h-0 flex-col lg:flex-row' : ''}`}>
        <nav
          aria-label="Areas"
          className="shrink-0 border-b border-line bg-panel lg:sticky lg:top-0 lg:h-screen lg:w-52 lg:border-b-0 lg:border-r"
        >
          <div className="flex items-center gap-2 px-4 py-3 lg:py-4">
            <Link
              href="/"
              className="rounded-sm text-[11px] font-semibold uppercase tracking-[0.18em] text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            >
              ARIA
            </Link>
          </div>
          <ul className="flex list-none gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:gap-0.5 lg:overflow-visible lg:px-2 lg:pb-4">
            {AREAS.map((a) => {
              const active = pathname === a.href || pathname.startsWith(a.href + '/')
              if (a.soon) {
                return (
                  <li key={a.href}>
                    <span
                      aria-disabled="true"
                      title="Not built yet"
                      className="block cursor-not-allowed whitespace-nowrap rounded-sm px-3 py-2 opacity-40 lg:whitespace-normal"
                    >
                      <span className="block text-xs">{a.label}</span>
                      <span className="hidden text-[10px] text-ink-faint lg:block">{a.blurb}</span>
                    </span>
                  </li>
                )
              }
              return (
                <li key={a.href}>
                  <Link
                    href={a.href}
                    aria-current={active ? 'page' : undefined}
                    className={`block whitespace-nowrap rounded-sm border-l-2 px-3 py-2 transition-colors lg:whitespace-normal focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent ${
                      active
                        ? 'border-accent bg-panel-2 text-accent'
                        : 'border-transparent text-ink hover:bg-panel-2'
                    }`}
                  >
                    <span className="block text-xs">{a.label}</span>
                    <span className="hidden text-[10px] text-ink-faint lg:block">{a.blurb}</span>
                  </Link>
                </li>
              )
            })}
          </ul>
        </nav>

        <main className={`min-w-0 flex-1 ${flush ? "flex min-h-0 flex-col" : ""}`}>
          <header className="flex shrink-0 flex-wrap items-baseline gap-x-5 gap-y-2 border-b border-line px-4 py-3">
            <div className="text-[11px] uppercase tracking-[0.14em] text-ink-faint">
              ARIA / <b className="font-semibold text-accent">{area}</b>
            </div>
            <div className="ml-auto flex flex-wrap items-baseline gap-x-5 gap-y-1">{status}</div>
          </header>
          <div className={flush ? 'min-h-0 flex-1' : 'px-4 py-4'}>{children}</div>
        </main>
      </div>
    </div>
  )
}

export function StatusStat({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="text-[11px] tracking-[0.04em] text-ink-dim">
      {label} <span className="tnum text-ink">{children}</span>
    </span>
  )
}
