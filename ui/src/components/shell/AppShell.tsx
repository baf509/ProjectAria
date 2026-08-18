/**
 * ARIA - Application shell
 *
 * Phase: UI / responsive rebuild (2026-08-17)
 *
 * The old shell's only structural breakpoint was `lg:` — below 1024px the
 * wrapper was `display:block`, so `main`'s `min-w-0 flex-1` was inert (an
 * ordinary block grows to its widest child) and the flush height chain used by
 * chat resolved to nothing. That single fact is why pages overflowed and why
 * `scrollIntoView` could drag a `100dvh` root by 2000px.
 *
 * Now: a flex column at EVERY width that becomes a row at lg. The shell owns
 * the page container, the gutters, the safe-area insets and the overflow clip,
 * so a page cannot forget them.
 */
'use client'

import { ReactNode } from 'react'
import { usePathname } from 'next/navigation'
import { matchArea } from '@/lib/areas'
import { Rail } from './Rail'
import { BottomTabs } from './BottomTabs'
import { TopBar } from './TopBar'
import { useVisualViewport } from './useVisualViewport'

export function AppShell({
  area,
  title,
  status,
  back,
  children,
  flush = false,
  width = 'content',
}: {
  /** Overrides the area derived from the pathname (rarely needed). */
  area?: string
  /** Screen title on the phone top bar; defaults to the area label. */
  title?: ReactNode
  status?: ReactNode
  /** Renders a back chip below lg — master/detail routes pass their list href. */
  back?: { href: string; label: string }
  children: ReactNode
  /** Full-bleed surfaces (chat thread, terminal) manage their own height. */
  flush?: boolean
  width?: 'content' | 'wide' | 'flush'
}) {
  const pathname = usePathname()
  const current = matchArea(pathname)
  const areaLabel = area ?? current?.label ?? 'ARIA'
  useVisualViewport()

  const container =
    width === 'flush'
      ? 'w-full'
      : width === 'wide'
        ? 'mx-auto w-full max-w-page'
        : 'mx-auto w-full max-w-page'

  return (
    <div
      className={`bg-ground font-mono text-body text-ink lg:grid lg:grid-cols-[13rem_minmax(0,1fr)] ${
        flush ? 'h-[var(--vvh,100dvh)] overflow-hidden' : 'min-h-dvh'
      }`}
    >
      <Rail />

      <div className={`flex min-w-0 flex-col ${flush ? 'h-full min-h-0' : 'min-h-dvh lg:min-h-0'}`}>
        <TopBar area={areaLabel} title={title} status={status} back={back} />

        <main
          data-shell-main
          className={
            flush
              ? 'flex min-h-0 min-w-0 flex-1 flex-col overflow-x-clip'
              : // The bottom padding clears the fixed tab bar plus the home
                // indicator; without it the last row of every list is
                // unreachable on a phone.
                'min-w-0 flex-1 overflow-x-clip px-safe py-3 pb-[calc(var(--tabbar-h)+var(--sab)+1rem)] lg:pb-6'
          }
        >
          <div className={`${container} min-w-0 ${flush ? 'flex min-h-0 flex-1 flex-col' : ''}`}>{children}</div>
        </main>

        <BottomTabs />
      </div>
    </div>
  )
}

/** A single labelled number in the top bar's status strip. */
export function StatusStat({
  label,
  children,
  tone = 'default',
}: {
  label: string
  children: ReactNode
  tone?: 'default' | 'warn' | 'ok'
}) {
  const tones = { default: 'text-ink', warn: 'text-gone', ok: 'text-live' }
  return (
    <span className="shrink-0 snap-start whitespace-nowrap text-micro tracking-[0.04em] text-ink-dim">
      {label} <span className={`tnum ${tones[tone]}`}>{children}</span>
    </span>
  )
}
