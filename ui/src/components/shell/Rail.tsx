'use client'

/**
 * Desktop rail (lg and up) — deliberately the same look as the 2026-08-05
 * design: all six areas with their blurbs, active item marked with the
 * accent left border. Hidden below lg, where the bottom tab bar takes over.
 */
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { AREAS, MORE_LINKS, matchArea } from '@/lib/areas'
import { BuildStamp } from './BuildStamp'
import { AdminKeyControl } from './AdminKeyControl'

export function Rail() {
  const pathname = usePathname()
  const active = matchArea(pathname)

  return (
    <nav
      aria-label="Areas"
      className="hidden shrink-0 border-r border-line bg-panel lg:sticky lg:top-0 lg:flex lg:h-dvh lg:w-52 lg:flex-col"
    >
      <div className="flex items-center gap-2 px-4 py-4">
        <Link
          href="/inbox"
          className="rounded-sm text-micro font-semibold uppercase tracking-[0.18em] text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          ARIA
        </Link>
      </div>
      <ul className="flex list-none flex-col gap-0.5 px-2 pb-4">
        {[...AREAS, ...MORE_LINKS].map((a) => {
          const isActive = active?.href === a.href || pathname === a.href
          return (
            <li key={a.href}>
              <Link
                href={a.href}
                aria-current={isActive ? 'page' : undefined}
                className={`block rounded-sm border-l-2 px-3 py-2 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent ${
                  isActive ? 'border-accent bg-panel-2 text-accent' : 'border-transparent text-ink hover:bg-panel-2'
                }`}
              >
                <span className="block text-label">{a.label}</span>
                <span className="block text-micro text-ink-faint">{a.blurb}</span>
              </Link>
            </li>
          )
        })}
      </ul>
      <div className="mt-auto flex flex-col gap-3 px-4 py-3">
        <AdminKeyControl />
        <BuildStamp />
      </div>
    </nav>
  )
}
