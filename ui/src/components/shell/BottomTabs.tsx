'use client'

/**
 * Phone navigation: five thumb tabs plus More.
 *
 * Replaces a 488px horizontal scroller in a 390px viewport where "Know" was cut
 * in half, "Autonomy" was entirely off-screen, and the links were 32px tall
 * with no scroll affordance. Five tabs at 375px are 75px each — an icon over a
 * 12px label with a 44px+ hit box.
 *
 * Decided with Ben 2026-08-17: 5 + More rather than 6, with Converse, Autonomy
 * and Benchmarks in the sheet. `phoneTab` in lib/areas.ts is the only place to
 * change if that turns out wrong.
 */
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { useState } from 'react'
import { PHONE_TABS, MORE_AREAS, MORE_ICON, matchArea } from '@/lib/areas'
import { Sheet } from '@/components/ui/controls'
import { BuildStamp } from './BuildStamp'
import { ThemeDensityControls } from './ThemeDensityControls'
import { AdminKeyControl } from './AdminKeyControl'

export function BottomTabs() {
  const pathname = usePathname()
  const active = matchArea(pathname)
  const [moreOpen, setMoreOpen] = useState(false)
  const moreActive = MORE_AREAS.some((a) => active?.href === a.href || pathname.startsWith(a.href))
  const MoreIcon = MORE_ICON

  return (
    <>
      <nav
        // Distinct from the desktop rail's landmark: two <nav>s labelled the
        // same thing are indistinguishable to a screen reader (and ambiguous to
        // anything else querying the page).
        aria-label="Primary"
        className="fixed inset-x-0 bottom-0 z-nav grid grid-cols-5 border-t border-line bg-panel pb-sab lg:hidden"
      >
        {PHONE_TABS.map((a) => {
          const isActive = active?.href === a.href
          const Icon = a.icon
          return (
            <Link
              key={a.href}
              href={a.href}
              aria-current={isActive ? 'page' : undefined}
              className={`flex min-h-tabbar flex-col items-center justify-center gap-0.5 border-t-2 px-1 pt-0.5 text-micro transition-colors [touch-action:manipulation] ${
                isActive ? 'border-accent text-accent' : 'border-transparent text-ink-dim'
              }`}
            >
              <Icon size={20} strokeWidth={1.75} aria-hidden="true" />
              <span className="truncate">{a.label}</span>
            </Link>
          )
        })}
        <button
          onClick={() => setMoreOpen(true)}
          aria-haspopup="dialog"
          aria-expanded={moreOpen}
          className={`flex min-h-tabbar flex-col items-center justify-center gap-0.5 border-t-2 px-1 pt-0.5 text-micro transition-colors [touch-action:manipulation] ${
            moreActive ? 'border-accent text-accent' : 'border-transparent text-ink-dim'
          }`}
        >
          <MoreIcon size={20} strokeWidth={1.75} aria-hidden="true" />
          <span>More</span>
        </button>
      </nav>

      <Sheet open={moreOpen} onClose={() => setMoreOpen(false)} title="More">
        <ul className="m-0 flex list-none flex-col gap-1 p-0">
          {MORE_AREAS.map((a) => {
            const Icon = a.icon
            return (
              <li key={a.href}>
                <Link
                  href={a.href}
                  onClick={() => setMoreOpen(false)}
                  className="flex min-h-control items-center gap-3 rounded-sm px-2 py-2 text-ink hover:bg-panel-2"
                >
                  <Icon size={18} strokeWidth={1.75} aria-hidden="true" className="shrink-0 text-ink-dim" />
                  <span className="min-w-0">
                    <span className="block text-body">{a.label}</span>
                    <span className="block text-micro text-ink-faint">{a.blurb}</span>
                  </span>
                </Link>
              </li>
            )
          })}
        </ul>
        <div className="mt-4 border-t border-line pt-3">
          <ThemeDensityControls />
        </div>
        <div className="mt-3 border-t border-line pt-3">
          <AdminKeyControl />
        </div>
        <div className="mt-3 border-t border-line pt-3">
          <BuildStamp />
        </div>
      </Sheet>
    </>
  )
}
