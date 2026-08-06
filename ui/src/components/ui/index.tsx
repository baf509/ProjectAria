/**
 * Shared UI primitives.
 *
 * Before this file the app had exactly one shared component, so every screen
 * inlined its own markup — which is why nothing looked consistent and why
 * dashboard/page.tsx grew to 68 KB. Anything used on more than one screen
 * belongs here.
 *
 * Everything is written against the design tokens in globals.css, so no
 * component branches on light/dark.
 */
'use client'

import { ReactNode, ButtonHTMLAttributes } from 'react'

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
    <section className={`rounded border border-line bg-panel ${className}`}>
      {title && (
        <header className="flex items-center gap-3 border-b border-line px-3.5 py-2.5">
          <h2 className="text-[10px] font-medium uppercase tracking-[0.16em] text-ink-faint">
            {title}
          </h2>
          {hint && <span className="truncate text-[10px] text-ink-faint">{hint}</span>}
          {actions && <div className="ml-auto flex gap-2">{actions}</div>}
        </header>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  )
}

/* ------------------------------------------------------------------ state */

export type ServerState = 'running' | 'exited' | 'absent' | 'external' | 'unknown'

/** Registry states collapsed to the four that change what you can DO. */
export function normalizeState(raw: string | undefined, weightsPresent = true): ServerState {
  if (!raw) return 'unknown'
  if (raw === 'running' || raw === 'paused' || raw === 'restarting') return 'running'
  if (raw === 'external') return 'external'
  if (!weightsPresent) return 'absent'
  if (raw === 'exited' || raw === 'not_created' || raw === 'created') return 'exited'
  return 'unknown'
}

const STATE_LABEL: Record<ServerState, string> = {
  running: 'running',
  exited: 'stopped',
  absent: 'weights absent',
  external: 'off-box',
  unknown: 'unknown',
}

const STATE_COLOR: Record<ServerState, string> = {
  running: 'text-live',
  exited: 'text-idle',
  absent: 'text-gone',
  external: 'text-idle',
  unknown: 'text-ink-faint',
}

const STATE_DOT: Record<ServerState, string> = {
  running: 'bg-live',
  exited: 'bg-idle',
  absent: 'bg-gone',
  external: 'bg-idle opacity-50',
  unknown: 'bg-ink-faint',
}

export function StatusDot({ state, className = '' }: { state: ServerState; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-block h-[7px] w-[7px] shrink-0 rounded-full ${STATE_DOT[state]} ${
        state === 'running' ? 'ring-[3px] ring-live/25' : ''
      } ${className}`}
    />
  )
}

export function StateChip({ state }: { state: ServerState }) {
  return (
    <span
      className={`rounded-sm border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.1em] ${STATE_COLOR[state]}`}
      style={{ borderColor: 'currentColor' }}
    >
      {STATE_LABEL[state]}
    </span>
  )
}

/* ---------------------------------------------------------------- controls */

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'default' | 'primary' | 'danger'
  busy?: boolean
}

export function Button({ variant = 'default', busy, children, className = '', ...rest }: BtnProps) {
  const base =
    'rounded-sm px-3 py-1.5 text-[11px] uppercase tracking-[0.08em] transition-colors ' +
    'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent ' +
    'disabled:cursor-not-allowed disabled:opacity-40'
  const variants = {
    default: 'border border-line bg-transparent text-ink hover:border-ink-faint',
    primary: 'border border-accent bg-accent font-semibold text-accent-ink hover:brightness-110',
    danger: 'border border-gone bg-transparent text-gone hover:bg-gone/10',
  }
  return (
    <button
      {...rest}
      disabled={rest.disabled || busy}
      className={`${base} ${variants[variant]} ${className}`}
    >
      {busy ? '···' : children}
    </button>
  )
}

/* -------------------------------------------------------------------- data */

export function KeyValue({ items }: { items: Array<{ k: string; v: ReactNode; title?: string }> }) {
  return (
    <dl className="mt-3.5 grid grid-cols-1 gap-x-6 border-t border-line sm:grid-cols-2 2xl:grid-cols-3">
      {items.map(({ k, v, title }) => (
        <div key={k} className="flex min-w-0 items-center justify-between gap-3 border-b border-line py-1.5">
          <dt className="whitespace-nowrap text-[10px] uppercase tracking-[0.06em] text-ink-faint">{k}</dt>
          <dd className="tnum min-w-0 truncate text-right text-xs" title={title}>
            {v}
          </dd>
        </div>
      ))}
    </dl>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="m-0 font-sans text-[13px] leading-relaxed text-ink-faint">{children}</p>
}

/** A budget bar. Segments are [width%, token] pairs drawn left to right. */
export function Meter({
  segments,
  left,
  right,
}: {
  segments: Array<{ pct: number; color: string; key: string }>
  left: ReactNode
  right: ReactNode
}) {
  return (
    <div>
      <div className="flex h-6 overflow-hidden rounded-sm bg-track">
        {segments.map((s) => (
          <div key={s.key} className={s.color} style={{ width: `${Math.max(0, Math.min(100, s.pct))}%` }} />
        ))}
      </div>
      <div className="tnum mt-1.5 flex justify-between text-[11px] text-ink-dim">
        <span>{left}</span>
        <span>{right}</span>
      </div>
    </div>
  )
}

export function Notice({
  tone = 'info',
  children,
}: {
  tone?: 'ok' | 'warn' | 'info'
  children: ReactNode
}) {
  const tones = {
    ok: 'border-live bg-live/10',
    warn: 'border-gone bg-gone/10',
    info: 'border-line bg-panel-2',
  }
  return (
    <div className={`mt-3 rounded-sm border px-3 py-2.5 font-sans text-xs leading-relaxed ${tones[tone]}`}>
      {children}
    </div>
  )
}

/** Small trend chart. Area + line + emphasised endpoint, per the chart rules. */
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
      <polygon points={`0,100 ${line} 100,100`} fill="var(--accent)" opacity="0.14" />
      <polyline
        points={line}
        fill="none"
        stroke="var(--accent)"
        strokeWidth="1.6"
        vectorEffect="non-scaling-stroke"
      />
      <circle cx={last[0]} cy={last[1]} r="2.2" fill="var(--accent)" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

/** Wide content must scroll inside its own box, never the page body. */
export function ScrollX({ children }: { children: ReactNode }) {
  return <div className="overflow-x-auto">{children}</div>
}
