/**
 * ARIA - Layout primitives
 *
 * Phase: UI / responsive rebuild
 * Purpose: containers that can only emit safe tracks, so an overflowing page
 * cannot be written by accident.
 *
 * The measured defect these exist to prevent: `grid gap-4 md:grid-cols-2` with
 * no base column made the single mobile column an implicit `auto` track, whose
 * floor is the widest item's min-content — 450px cards in a 390px viewport.
 * `Grid` always emits `minmax(0,1fr)` / `minmax(min(100%, X), 1fr)` instead.
 */
import { ReactNode, ElementType, CSSProperties } from 'react'

const cx = (...parts: Array<string | false | undefined>) => parts.filter(Boolean).join(' ')

/* -------------------------------------------------------------------- stack */

export function Stack({
  children,
  gap = 'gap',
  as: As = 'div',
  className = '',
}: {
  children: ReactNode
  gap?: 'gap' | 'sm' | 'lg' | 'none'
  as?: ElementType
  className?: string
}) {
  const gaps = { gap: 'gap-gap', sm: 'gap-2', lg: 'gap-6', none: 'gap-0' }
  return <As className={cx('flex min-w-0 flex-col', gaps[gap], className)}>{children}</As>
}

/* ------------------------------------------------------------------ cluster */

/**
 * A wrapping row. `nowrap` turns it into a snap scroller — the only sanctioned
 * way for a row of chips to exceed its container (and it carries the
 * `data-scroll-x` marker the overflow gate exempts).
 */
export function Cluster({
  children,
  gap = 'gap-2',
  align = 'items-center',
  justify = '',
  nowrap = false,
  className = '',
}: {
  children: ReactNode
  gap?: string
  align?: string
  justify?: string
  nowrap?: boolean
  className?: string
}) {
  if (nowrap) {
    return (
      <div
        data-scroll-x
        className={cx(
          'flex snap-x snap-mandatory overflow-x-auto overscroll-x-contain [scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
          gap,
          align,
          justify,
          className
        )}
      >
        {children}
      </div>
    )
  }
  return <div className={cx('flex min-w-0 flex-wrap', gap, align, justify, className)}>{children}</div>
}

/* --------------------------------------------------------------------- grid */

/**
 * `min` is the smallest a column may be BEFORE it drops to one column, and it
 * is wrapped in `min(100%, …)` so a column can never be wider than its
 * container regardless of content. This is the structural fix for the cockpit
 * overflow.
 */
export function Grid({
  children,
  min = '18rem',
  gap = 'gap-gap',
  className = '',
}: {
  children: ReactNode
  min?: string
  gap?: string
  className?: string
}) {
  return (
    <div
      className={cx('grid min-w-0', gap, className)}
      style={
        {
          gridTemplateColumns: `repeat(auto-fit, minmax(min(100%, ${min}), 1fr))`,
        } as CSSProperties
      }
    >
      {children}
    </div>
  )
}

/** Explicit column counts, always with a base of one. */
export function Columns({
  children,
  sm,
  lg,
  xl,
  gap = 'gap-gap',
  className = '',
}: {
  children: ReactNode
  sm?: 2 | 3
  lg?: 2 | 3 | 4
  xl?: 2 | 3 | 4
  gap?: string
  className?: string
}) {
  const smC = sm === 2 ? 'sm:grid-cols-2' : sm === 3 ? 'sm:grid-cols-3' : ''
  const lgC = lg === 2 ? 'lg:grid-cols-2' : lg === 3 ? 'lg:grid-cols-3' : lg === 4 ? 'lg:grid-cols-4' : ''
  const xlC = xl === 2 ? 'xl:grid-cols-2' : xl === 3 ? 'xl:grid-cols-3' : xl === 4 ? 'xl:grid-cols-4' : ''
  return <div className={cx('grid min-w-0 grid-cols-1', gap, smC, lgC, xlC, className)}>{children}</div>
}

/* ---------------------------------------------------------------------- row */

/**
 * The list-row contract: marker · primary (shrinkable) · trailing (never
 * shrinks, never wraps). Used by fleet rows, shell rows, inbox rows, service
 * rows — so a touch-size fix lands in one place.
 */
export function Row({
  marker,
  children,
  trailing,
  className = '',
  as: As = 'div',
  ...rest
}: {
  marker?: ReactNode
  children: ReactNode
  trailing?: ReactNode
  className?: string
  as?: ElementType
  [key: string]: unknown
}) {
  return (
    <As
      className={cx(
        'grid min-h-row w-full min-w-0 grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2.5 text-left',
        className
      )}
      {...rest}
    >
      <span className="flex shrink-0 items-center">{marker}</span>
      <span className="min-w-0">{children}</span>
      <span className="tnum shrink-0 whitespace-nowrap text-micro text-ink-dim">{trailing}</span>
    </As>
  )
}

/* ------------------------------------------------------------------ scrollx */

/** The ONLY sanctioned horizontal scroller; the overflow gate exempts its subtree. */
export function ScrollX({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div
      data-scroll-x
      className={cx('overflow-x-auto overscroll-x-contain [scrollbar-gutter:stable]', className)}
    >
      {children}
    </div>
  )
}
