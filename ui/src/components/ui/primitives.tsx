/**
 * ARIA - Shared UI primitives (server-safe)
 *
 * NOTHING in this file is a client component. That is deliberate: the previous
 * single `'use client'` at the top of ui/index.tsx meant Card, KeyValue and
 * Meter could not render inside a Server Component, which is what made server
 * rendering impossible app-wide. Interactive primitives live in ./controls.tsx.
 *
 * Everything is written against the design tokens, so no component branches on
 * light/dark, and sizes come from the density variables rather than pixel
 * literals — a 44px touch target on the phone costs the laptop nothing.
 */
import { ReactNode } from 'react'

const cx = (...p: Array<string | false | undefined>) => p.filter(Boolean).join(' ')

/* -------------------------------------------------------------- containers */

export function Card({
  title,
  hint,
  actions,
  children,
  className = '',
  bodyClassName = 'p-3.5',
}: {
  title?: ReactNode
  hint?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
}) {
  return (
    <section className={cx('@container min-w-0 overflow-x-clip rounded border border-line bg-panel', className)}>
      {title && (
        <header className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-3.5 py-2.5">
          <h2 className="text-micro font-medium uppercase tracking-[0.16em] text-ink-faint">{title}</h2>
          {hint && <span className="min-w-0 truncate text-micro text-ink-faint">{hint}</span>}
          {actions && <div className="ml-auto flex flex-wrap gap-2">{actions}</div>}
        </header>
      )}
      <div className={cx('min-w-0', bodyClassName)}>{children}</div>
    </section>
  )
}

/* ------------------------------------------------------------------- state */

export type ServerState =
  | 'running'
  | 'ready'
  | 'loading'
  | 'failed'
  | 'asleep'
  | 'exited'
  | 'absent'
  | 'external'
  | 'unknown'

/** Registry vocabulary collapsed to the states that change what you can DO. */
export function normalizeState(raw: string | undefined, weightsPresent = true): ServerState {
  if (!raw) return 'unknown'
  if (raw === 'running' || raw === 'paused') return 'running'
  if (raw === 'restarting' || raw === 'activating' || raw === 'starting') return 'loading'
  // The registry emits 'ready' for "startable, but its unit has not been
  // materialised yet" — a STOPPED state. Presenting it as a live green (as the
  // first cut did) says a model is up when nothing is running.
  if (raw === 'ready') return 'ready'
  if (raw === 'failed' || raw === 'error') return 'failed'
  if (raw === 'asleep' || raw === 'suspended') return 'asleep'
  if (raw === 'external') return 'external'
  if (!weightsPresent) return 'absent'
  if (raw === 'exited' || raw === 'not_created' || raw === 'created' || raw === 'stopped') return 'exited'
  return 'unknown'
}

const STATE_LABEL: Record<ServerState, string> = {
  running: 'running',
  ready: 'not created',
  loading: 'loading',
  failed: 'failed',
  asleep: 'asleep',
  exited: 'stopped',
  absent: 'weights absent',
  external: 'off-box',
  unknown: 'unknown',
}

const STATE_COLOR: Record<ServerState, string> = {
  running: 'text-live',
  ready: 'text-idle',
  loading: 'text-accent',
  failed: 'text-gone',
  asleep: 'text-idle',
  exited: 'text-idle',
  absent: 'text-gone',
  external: 'text-idle',
  unknown: 'text-ink-faint',
}

const STATE_DOT: Record<ServerState, string> = {
  running: 'bg-live',
  ready: 'bg-idle',
  loading: 'bg-accent',
  failed: 'bg-gone',
  asleep: 'bg-idle',
  exited: 'bg-idle',
  absent: 'bg-gone',
  external: 'bg-idle opacity-60',
  unknown: 'bg-ink-mute',
}

export function StatusDot({ state, className = '' }: { state: ServerState; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cx(
        'inline-block h-[7px] w-[7px] shrink-0 rounded-full',
        STATE_DOT[state],
        state === 'running' && 'ring-[3px] ring-live/25',
        state === 'loading' && 'ring-[3px] ring-accent/25',
        className
      )}
    />
  )
}

export function StateChip({ state, note }: { state: ServerState; note?: ReactNode }) {
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-sm border px-1.5 py-0.5 text-micro font-semibold uppercase tracking-[0.1em]',
        STATE_COLOR[state]
      )}
      style={{ borderColor: 'currentColor' }}
    >
      {STATE_LABEL[state]}
      {/* Weight, not opacity, carries the de-emphasis. `opacity-80` multiplied
          the chip's colour down to 3.34:1 on every StateChip that carries a
          note — the same "dim it to de-emphasise it" mistake this rebuild
          removed from the cockpit cards, reintroduced inside a primitive where
          it affected every page at once. */}
      {note ? <span className="font-normal">{note}</span> : null}
    </span>
  )
}

export function Chip({
  children,
  tone = 'neutral',
  className = '',
}: {
  children: ReactNode
  tone?: 'neutral' | 'ok' | 'warn' | 'accent'
  className?: string
}) {
  const tones = {
    neutral: 'border-line bg-panel-2 text-ink-dim',
    ok: 'border-live/40 bg-live/10 text-live',
    warn: 'border-gone/40 bg-gone/10 text-gone',
    accent: 'border-accent/40 bg-accent/10 text-accent',
  }
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-sm border px-1.5 py-0.5 text-micro uppercase tracking-[0.08em]',
        tones[tone],
        className
      )}
    >
      {children}
    </span>
  )
}

/* -------------------------------------------------------------------- text */

/**
 * Prose. Wraps machine strings instead of widening the page, clamps long
 * registry essays, and never relies on a hover `title=` (there is no hover on a
 * phone) — the clamped case gets a real disclosure from ./controls.
 */
export function Text({
  children,
  clamp,
  className = '',
}: {
  children: ReactNode
  clamp?: 2 | 3 | 4
  className?: string
}) {
  const clamps = { 2: 'line-clamp-2', 3: 'line-clamp-3', 4: 'line-clamp-4' }
  return (
    <p className={cx('m-0 max-w-prose wrap-anywhere font-sans text-prose text-ink-dim', clamp && clamps[clamp], className)}>
      {children}
    </p>
  )
}

/** Identifiers, paths, URLs, slugs: mono, breakable, never layout-defining. */
export function Code({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <code className={cx('break-all font-mono text-micro text-ink-dim', className)}>{children}</code>
  )
}

/* -------------------------------------------------------------------- data */

export type KeyValueItem = {
  k: string
  v: ReactNode
  /** num = tabular + nowrap; ident = break-all mono; prose = wrapping sans. */
  kind?: 'num' | 'ident' | 'prose'
  title?: string
}

/**
 * Two layouts. `table` is the one-line dt/dd for numbers; `stack` puts the label
 * above a wrapping value and is what registry prose and 122-char paths need —
 * they used to be `truncate`d with a hover tooltip that a phone cannot show.
 * Columns come from a CONTAINER query, so a card in a narrow column behaves by
 * its own width rather than the viewport's.
 */
export function KeyValue({
  items,
  layout = 'table',
  className = '',
}: {
  items: KeyValueItem[]
  layout?: 'table' | 'stack'
  className?: string
}) {
  if (layout === 'stack') {
    return (
      <dl className={cx('m-0 mt-3 grid grid-cols-1 gap-3 border-t border-line pt-3 @2xl:grid-cols-2', className)}>
        {items.map(({ k, v, kind }) => (
          <div key={k} className="min-w-0">
            <dt className="text-micro uppercase tracking-[0.08em] text-ink-faint">{k}</dt>
            <dd
              className={cx(
                'mt-0.5 min-w-0 text-body',
                kind === 'ident' && 'break-all font-mono',
                kind === 'prose' && 'font-sans text-prose wrap-anywhere',
                kind === 'num' && 'tnum'
              )}
            >
              {v}
            </dd>
          </div>
        ))}
      </dl>
    )
  }
  return (
    <dl className={cx('m-0 mt-3.5 grid grid-cols-1 gap-x-6 border-t border-line @2xl:grid-cols-2 @5xl:grid-cols-3', className)}>
      {items.map(({ k, v, kind, title }) => (
        <div key={k} className="flex min-w-0 items-center justify-between gap-3 border-b border-line py-1.5">
          <dt className="whitespace-nowrap text-micro uppercase tracking-[0.06em] text-ink-faint">{k}</dt>
          <dd
            className={cx(
              'min-w-0 text-right text-label',
              kind === 'ident' ? 'break-all font-mono' : 'tnum truncate'
            )}
            title={title}
          >
            {v}
          </dd>
        </div>
      ))}
    </dl>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="m-0 font-sans text-prose leading-relaxed text-ink-faint">{children}</p>
}

export function Notice({
  tone = 'info',
  children,
  className = '',
}: {
  tone?: 'ok' | 'warn' | 'info'
  children: ReactNode
  className?: string
}) {
  const tones = {
    ok: 'border-live/50 bg-live/10',
    warn: 'border-gone/50 bg-gone/10',
    info: 'border-line bg-panel-2',
  }
  return (
    <div className={cx('rounded-sm border px-3 py-2.5 font-sans text-prose leading-relaxed text-ink', tones[tone], className)}>
      {children}
    </div>
  )
}

/** A budget bar. Segments are drawn left to right. */
export function Meter({
  segments,
  left,
  right,
  label,
}: {
  segments: Array<{ pct: number; color: string; key: string }>
  left: ReactNode
  right: ReactNode
  label?: string
}) {
  return (
    <div role={label ? 'img' : undefined} aria-label={label}>
      <div className="flex h-6 overflow-hidden rounded-sm bg-track">
        {segments.map((s) => (
          <div key={s.key} className={s.color} style={{ width: `${Math.max(0, Math.min(100, s.pct))}%` }} />
        ))}
      </div>
      <div className="tnum mt-1.5 flex justify-between gap-2 text-micro text-ink-dim">
        <span className="min-w-0 truncate">{left}</span>
        <span className="shrink-0">{right}</span>
      </div>
    </div>
  )
}

/** Small trend chart: area + line + emphasised endpoint. */
export function Sparkline({ values, label }: { values: number[]; label?: string }) {
  if (values.length < 2) return null
  const max = Math.max(...values)
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * 100
    const y = 100 - (max > 0 ? (v / max) * 88 : 0) - 6
    return [x, y] as const
  })
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  const last = pts[pts.length - 1]
  return (
    <svg
      className="mt-2 block h-11 w-full"
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : 'true'}
    >
      <polygon points={`0,100 ${line} 100,100`} fill="rgb(var(--accent-rgb))" opacity="0.14" />
      <polyline points={line} fill="none" stroke="rgb(var(--accent-rgb))" strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
      <circle cx={last[0]} cy={last[1]} r="2.2" fill="rgb(var(--accent-rgb))" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

/**
 * Skeletons occupy the FINAL size, so data landing causes no layout shift and a
 * pending panel is a visible panel rather than a white gap.
 */
export function Skeleton({ rows = 3, className = '' }: { rows?: number; className?: string }) {
  return (
    <div className={cx('flex flex-col gap-2', className)} aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-row animate-pulse rounded-sm bg-panel-2" />
      ))}
    </div>
  )
}

/** "updated 12s ago" — always rendered next to cached data. */
export function Freshness({ at, stale }: { at?: number | null; stale?: boolean }) {
  if (!at) return null
  const secs = Math.max(0, Math.round((Date.now() - at) / 1000))
  const text = secs < 60 ? `${secs}s` : `${Math.round(secs / 60)}m`
  return (
    <span className={cx('tnum whitespace-nowrap text-micro', stale ? 'text-gone' : 'text-ink-faint')}>
      {stale ? 'stale · ' : 'updated '}
      {text} ago
    </span>
  )
}
