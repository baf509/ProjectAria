'use client'

/**
 * Top bar: breadcrumb + optional back chip + a status strip.
 *
 * On a phone the old header's `flex-wrap` turned six status stats into four
 * half-empty rows (128px, 15% of the viewport) that then scrolled away. Here
 * the strip is a single snap scroller with alarms first, and the bar is sticky
 * so the numbers stay reachable.
 */
import Link from 'next/link'
import { ReactNode } from 'react'
import { ChevronLeft } from 'lucide-react'

export function TopBar({
  area,
  title,
  status,
  back,
}: {
  area: string
  title?: ReactNode
  status?: ReactNode
  back?: { href: string; label: string }
}) {
  return (
    <header className="sticky top-0 z-30 shrink-0 border-b border-line bg-ground/95 pt-sat backdrop-blur supports-[backdrop-filter]:bg-ground/80">
      <div className="flex min-h-topbar min-w-0 items-center gap-2 px-safe py-1.5">
        {back && (
          <Link
            href={back.href}
            className="-ml-1 inline-flex min-h-control min-w-control items-center gap-1 rounded-sm px-1 text-micro text-ink-dim hover:text-ink lg:hidden"
          >
            <ChevronLeft size={16} aria-hidden="true" />
            <span className="sr-only">{back.label}</span>
          </Link>
        )}
        <div className="min-w-0 truncate text-micro uppercase tracking-[0.14em] text-ink-faint">
          ARIA / <b className="font-semibold text-accent">{area}</b>
          {title ? <span className="ml-2 normal-case tracking-normal text-ink">{title}</span> : null}
        </div>
        {status && (
          <div
            data-scroll-x
            className="ml-auto flex snap-x gap-x-4 overflow-x-auto overscroll-x-contain py-0.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden lg:flex-wrap lg:overflow-visible"
          >
            {status}
          </div>
        )}
      </div>
    </header>
  )
}
