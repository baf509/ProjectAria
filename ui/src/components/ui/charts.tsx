'use client'

/**
 * ARIA - chart primitives (client)
 *
 * Hand-drawn SVG rather than a charting library: the payloads here are tens of
 * points, the theme is already expressed as tokens, and every library we'd pull
 * in wants to own colour and typography — which is the one thing this codebase
 * does not delegate.
 *
 * Three rules these components exist to enforce, because they are the ones that
 * are easy to get wrong once and then never notice:
 *
 * 1. `null` is a GAP, never a zero. A backend that does not report prompt-cache
 *    reuse is not a backend with no reuse, and the API is careful to return
 *    null there — a chart that draws it on the floor throws that away.
 * 2. Colour follows the ENTITY, not its rank. `seriesColor` keys off the series
 *    name's position in a stable list, so filtering or a window change never
 *    repaints the survivors.
 * 3. Text wears text tokens. A value label is `ink`, never the series colour;
 *    the mark beside it is what carries identity.
 *
 * Sizes are SVG user units in a fixed viewBox, so they scale with the card. The
 * `fontSize` attribute is used rather than a utility class because a class
 * would be an arbitrary-size violation and would NOT scale with the viewBox.
 */
import { ReactNode, useCallback, useEffect, useId, useRef, useState } from 'react'

const cx = (...p: Array<string | false | undefined>) => p.filter(Boolean).join(' ')

/** The categorical slots, in fixed order. Status hues are deliberately absent. */
export const SERIES_TOKENS = ['cat-1', 'cat-2', 'cat-3', 'cat-4'] as const
const OTHER_TOKEN = 'ink-mute'

const token = (name: string) => `rgb(var(--${name}-rgb))`

/**
 * The colour for one series, by its index in the chart's stable series list.
 * Anything past the four slots — and anything the API folded into `Other` — is
 * the neutral, which is what stops a ninth model from inventing a ninth hue.
 */
export function seriesColor(index: number, name?: string): string {
  if (name === 'Other' || index >= SERIES_TOKENS.length) return token(OTHER_TOKEN)
  return token(SERIES_TOKENS[index])
}

/* ------------------------------------------------------------------ sizing */

/** Axis and label type inside a chart. 12px is the touch type floor. */
const AXIS_FS = 12

/**
 * The chart's own pixel width, so the SVG can be drawn 1:1.
 *
 * A fixed viewBox scaled into a narrow card shrinks its TEXT along with its
 * geometry: a 720-unit chart in a 330px phone card renders 11px labels at about
 * 5px, which is below the type floor everywhere else in the app enforces and
 * simply cannot be read. Measuring instead means one user unit is one pixel, so
 * `fontSize` means what it says at every width.
 */
function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const apply = (w: number) => setWidth((prev) => (Math.abs(prev - w) < 1 ? prev : Math.round(w)))
    apply(el.getBoundingClientRect().width)
    const ro = new ResizeObserver((entries) => apply(entries[0].contentRect.width))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return { ref, width }
}

/* ----------------------------------------------------------------- tooltip */

type TipState = { x: number; y: number; body: ReactNode } | null

function useTooltip() {
  const [tip, setTip] = useState<TipState>(null)
  const show = useCallback((e: { clientX: number; clientY: number }, body: ReactNode) => {
    setTip({ x: e.clientX, y: e.clientY, body })
  }, [])
  const hide = useCallback(() => setTip(null), [])
  return { tip, show, hide }
}

function Tooltip({ tip }: { tip: TipState }) {
  if (!tip) return null
  // Nudged up and right of the pointer, then clamped by the viewport so a mark
  // near the right edge does not push the panel off-screen.
  const left = Math.min(tip.x + 14, (typeof window === 'undefined' ? 9999 : window.innerWidth) - 232)
  return (
    <div
      role="status"
      className="pointer-events-none fixed z-30 max-w-[14rem] rounded-sm border border-line bg-panel px-2 py-1.5 font-mono text-micro text-ink shadow-lg"
      style={{ left: Math.max(8, left), top: Math.max(8, tip.y - 12) }}
    >
      {tip.body}
    </div>
  )
}

function TipRow({ color, label, value }: { color?: string; label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      {color && <i aria-hidden="true" className="mt-1 h-2 w-2 shrink-0 rounded-sm" style={{ background: color }} />}
      <span className="min-w-0 truncate text-ink-faint">{label}</span>
      <span className="tnum ml-auto font-semibold">{value}</span>
    </div>
  )
}

/* ------------------------------------------------------------------ legend */

/** Identity is never colour alone: every series is named here as well as drawn. */
export function ChartLegend({ names }: { names: string[] }) {
  if (names.length < 2) return null
  return (
    <ul className="m-0 mt-2.5 flex list-none flex-wrap gap-x-4 gap-y-1 p-0 font-mono text-micro text-ink-dim">
      {names.map((n, i) => (
        <li key={n} className="flex min-w-0 items-center gap-1.5">
          <i aria-hidden="true" className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: seriesColor(i, n) }} />
          <span className="truncate">{n}</span>
        </li>
      ))}
    </ul>
  )
}

/* ------------------------------------------------------------ stacked bars */

export type StackedBucket = { label: string; parts: Record<string, number>; total: number }

/**
 * Daily totals split by one dimension. Segments carry a 2px surface gap so a
 * stack reads as parts rather than a gradient, and only the bars that carry the
 * story are direct-labelled — a number on every bar is noise.
 */
export function StackedBars({
  buckets,
  series,
  format,
  labelThreshold = 0.3,
  ariaLabel,
}: {
  buckets: StackedBucket[]
  series: string[]
  format: (v: number) => string
  /** Direct-label bars at or above this share of the tallest. */
  labelThreshold?: number
  ariaLabel: string
}) {
  const { tip, show, hide } = useTooltip()
  const { ref, width } = useWidth<HTMLDivElement>()
  const H = 260, ML = 46, MR = 8, MT = 18, MB = 26
  // Reserve the height before measuring so data landing causes no layout shift.
  if (!buckets.length) return null
  if (width === 0) return <div ref={ref} style={{ height: H }} />
  const W = width
  const iw = W - ML - MR, ih = H - MT - MB
  const peak = Math.max(...buckets.map((b) => b.total), 1)
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * peak)
  const y = (v: number) => MT + ih - (v / peak) * ih
  const step = iw / buckets.length
  const bw = Math.min(46, step * 0.62)
  // Label density follows the ACTUAL width, not just the bucket count: a month
  // of daily bars fits twelve labels on a laptop and four on a phone.
  const everyNth = Math.max(1, Math.ceil(buckets.length / Math.max(3, Math.floor(iw / 56))))

  return (
    <div ref={ref}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="block" role="img" aria-label={ariaLabel}>
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={ML} x2={ML + iw} y1={y(t)} y2={y(t)} stroke={token('line')} strokeWidth={1} />
            <text x={ML - 7} y={y(t) + 4} textAnchor="end" fontSize={AXIS_FS} fill={token('ink-faint')} className="tnum">
              {t === 0 ? '0' : format(t)}
            </text>
          </g>
        ))}
        {buckets.map((b, i) => {
          const cxp = ML + step * i + step / 2
          let acc = 0
          return (
            <g key={b.label}>
              {series.map((name, si) => {
                const v = b.parts[name] ?? 0
                if (v <= 0) return null
                const top = y(acc + v), bottom = y(acc)
                acc += v
                const h = Math.max(1.5, bottom - top - 2)
                return (
                  <rect
                    key={name}
                    x={cxp - bw / 2} y={top} width={bw} height={h} rx={1.5}
                    fill={seriesColor(si, name)}
                    onMouseMove={(e) => show(e, (
                      <>
                        <div className="mb-1 text-ink-faint">{b.label} · {format(b.total)}</div>
                        {series.filter((s) => (b.parts[s] ?? 0) > 0).map((s) => (
                          <TipRow key={s} color={seriesColor(series.indexOf(s), s)} label={s} value={format(b.parts[s])} />
                        ))}
                      </>
                    ))}
                    onMouseLeave={hide}
                  />
                )
              })}
              {b.total >= peak * labelThreshold && (
                <text x={cxp} y={y(b.total) - 6} textAnchor="middle" fontSize={AXIS_FS}
                      fontWeight={600} fill={token('ink-faint')} className="tnum">
                  {format(b.total)}
                </text>
              )}
              {i % everyNth === 0 && (
                <text x={cxp} y={H - 8} textAnchor="middle" fontSize={AXIS_FS} fill={token('ink-faint')} className="tnum">
                  {b.label}
                </text>
              )}
            </g>
          )
        })}
      </svg>
      <Tooltip tip={tip} />
    </div>
  )
}

/* -------------------------------------------------------------- trend line */

export type LinePoint = { label: string; value: number | null }

/**
 * A single measure over time, where `null` breaks the line.
 *
 * The break is the entire point. `/usage/series` returns a null hit rate for a
 * bucket no backend reported reuse for, and drawing that at zero would restate
 * the exact bug `cache_reporting` was added to fix. Unreported buckets get a
 * baseline tick instead, so "we did not measure" stays visible rather than
 * silently becoming "we measured nothing".
 */
export function TrendLine({
  points,
  format,
  domain,
  ariaLabel,
  color = token('cat-2'),
}: {
  points: LinePoint[]
  format: (v: number) => string
  domain: [number, number]
  ariaLabel: string
  color?: string
}) {
  const { tip, show, hide } = useTooltip()
  const { ref, width } = useWidth<HTMLDivElement>()
  const clip = useId().replace(/:/g, '')
  const H = 200, ML = 44, MR = 10, MT = 14, MB = 24
  if (points.length < 2) return null
  if (width === 0) return <div ref={ref} style={{ height: H }} />
  const W = width
  const iw = W - ML - MR, ih = H - MT - MB
  const [lo, hi] = domain
  const x = (i: number) => ML + (i / (points.length - 1)) * iw
  const y = (v: number) => MT + ih - ((v - lo) / (hi - lo || 1)) * ih
  const ticks = [0, 0.5, 1].map((f) => lo + f * (hi - lo))

  let d = ''
  let pen = false
  points.forEach((p, i) => {
    if (p.value === null) { pen = false; return }
    d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p.value).toFixed(1)} `
    pen = true
  })

  return (
    <div ref={ref}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="block" role="img" aria-label={ariaLabel}>
        <clipPath id={clip}><rect x={ML} y={MT - 4} width={iw} height={ih + 8} /></clipPath>
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={ML} x2={ML + iw} y1={y(t)} y2={y(t)} stroke={token('line')} strokeWidth={1} />
            <text x={ML - 6} y={y(t) + 4} textAnchor="end" fontSize={AXIS_FS} fill={token('ink-faint')} className="tnum">
              {format(t)}
            </text>
          </g>
        ))}
        <g clipPath={`url(#${clip})`}>
          <path d={d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        </g>
        {points.map((p, i) =>
          p.value === null ? (
            <rect key={i} x={x(i) - 1} y={MT + ih - 5} width={2} height={5} fill={token('ink-mute')} />
          ) : (
            <g key={i}>
              <circle cx={x(i)} cy={y(p.value)} r={2.4} fill={color} stroke={token('panel')} strokeWidth={1.4} />
              <circle
                cx={x(i)} cy={y(p.value)} r={9} fill="transparent"
                onMouseMove={(e) => show(e, (
                  <>
                    <div className="mb-1 text-ink-faint">{p.label}</div>
                    <TipRow color={color} label="value" value={format(p.value as number)} />
                  </>
                ))}
                onMouseLeave={hide}
              />
            </g>
          )
        )}
        <text x={ML} y={H - 6} fontSize={AXIS_FS} fill={token('ink-faint')} className="tnum">{points[0].label}</text>
        <text x={ML + iw} y={H - 6} textAnchor="end" fontSize={AXIS_FS} fill={token('ink-faint')} className="tnum">
          {points[points.length - 1].label}
        </text>
      </svg>
      <Tooltip tip={tip} />
    </div>
  )
}

/* ------------------------------------------------------------ ranked bars */

export type RankedRow = { name: string; value: number; note?: string }

/** Top-N by magnitude. One measure, one colour — rank is the y position. */
export function RankedBars({
  rows,
  format,
  ariaLabel,
}: {
  rows: RankedRow[]
  format: (v: number) => string
  ariaLabel: string
}) {
  const { tip, show, hide } = useTooltip()
  if (!rows.length) return null
  const peak = Math.max(...rows.map((r) => r.value), 1)

  // HTML rather than SVG: the name is the widest thing here and needs to
  // truncate to the container, the value needs to stay on the type scale at
  // every width, and both are things CSS does better than a viewBox.
  return (
    <>
      <ul className="m-0 flex list-none flex-col gap-1.5 p-0" aria-label={ariaLabel}>
        {rows.map((r) => (
          <li key={r.name} className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-3 gap-y-1">
            <span className="min-w-0 truncate font-mono text-micro text-ink-dim" title={r.name}>
              {r.name}
            </span>
            <span className="tnum shrink-0 font-mono text-micro text-ink">{format(r.value)}</span>
            <span
              className="col-span-2 h-2 overflow-hidden rounded-sm bg-track"
              onMouseMove={(e) => show(e, (
                <>
                  <div className="mb-1 break-all text-ink-faint">{r.name}</div>
                  <TipRow label="tokens" value={format(r.value)} />
                  {r.note && <TipRow label="detail" value={r.note} />}
                </>
              ))}
              onMouseLeave={hide}
            >
              <span
                className="block h-full rounded-sm"
                style={{ width: `${Math.max(1, (r.value / peak) * 100)}%`, background: token('cat-2') }}
              />
            </span>
          </li>
        ))}
      </ul>
      <Tooltip tip={tip} />
    </>
  )
}

/* ------------------------------------------------------------- trend card */

/**
 * One metric's recent history, headed by `now · avg · peak · window`.
 *
 * The header is the component's reason to exist: four numbers answer "is this
 * normal?" before the reader interprets a single pixel of the line, which a
 * bare sparkline cannot do.
 */
export function TrendCard({
  label,
  values,
  now,
  format,
  domain,
  window: windowLabel,
  threshold,
  thresholdNote,
  color = token('cat-2'),
  className = '',
}: {
  label: string
  values: number[]
  now: number | null
  format: (v: number) => string
  domain: [number, number]
  window: string
  threshold?: number
  thresholdNote?: string
  color?: string
  className?: string
}) {
  const W = 240, H = 54
  const [lo, hi] = domain
  const finite = values.filter((v) => Number.isFinite(v))
  const avg = finite.length ? finite.reduce((a, b) => a + b, 0) / finite.length : null
  const peak = finite.length ? Math.max(...finite) : null
  const x = (i: number) => (i / Math.max(1, values.length - 1)) * W
  const y = (v: number) => H - 3 - ((v - lo) / (hi - lo || 1)) * (H - 8)
  const line = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')

  return (
    <div className={cx('min-w-0 rounded-sm border border-line bg-panel-2 p-2.5', className)}>
      <div className="mb-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="text-micro uppercase tracking-[0.13em] text-ink-faint">{label}</span>
        <span className="tnum text-label font-semibold text-ink">{now === null ? '—' : format(now)}</span>
        <span className="tnum ml-auto whitespace-nowrap text-micro text-ink-faint">
          {avg === null ? 'no samples' : `avg ${format(avg)} · peak ${format(peak as number)} · ${windowLabel}`}
        </span>
      </div>
      {values.length > 1 ? (
        <svg
          viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="block h-14 w-full"
          role="img"
          aria-label={`${label}: now ${now === null ? 'unknown' : format(now)}, average ${avg === null ? 'unknown' : format(avg)}, peak ${peak === null ? 'unknown' : format(peak)}, over ${windowLabel}`}
        >
          {threshold !== undefined && (
            <line x1={0} x2={W} y1={y(threshold)} y2={y(threshold)} stroke={token('gone')} strokeWidth={1} strokeDasharray="3 3" />
          )}
          <polygon points={`0,${H} ${line} ${W},${H}`} fill={color} opacity={0.13} />
          <polyline points={line} fill="none" stroke={color} strokeWidth={1.6} vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
          {now !== null && <circle cx={W} cy={y(now)} r={2.6} fill={color} vectorEffect="non-scaling-stroke" />}
        </svg>
      ) : (
        <p className="m-0 py-3 font-mono text-micro text-ink-faint">No history retained yet.</p>
      )}
      {thresholdNote && <p className="m-0 mt-1 font-mono text-micro text-ink-faint">{thresholdNote}</p>}
    </div>
  )
}
