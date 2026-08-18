/**
 * ARIA - relative time and durations
 *
 * Replaces three divergent copies of `relativeTime` (cockpit, cockpit/[slug],
 * shells) that formatted the same data three ways.
 */

const RTF = new Intl.RelativeTimeFormat('en', { numeric: 'auto', style: 'narrow' })

const UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
  ['second', 1],
]

/** "3h ago", "just now", "" for missing input. Never throws on bad data. */
export function relativeTime(value?: string | number | Date | null): string {
  if (value === null || value === undefined || value === '') return ''
  const then = value instanceof Date ? value.getTime() : new Date(value).getTime()
  if (!Number.isFinite(then)) return ''
  const deltaSec = (then - Date.now()) / 1000
  const abs = Math.abs(deltaSec)
  if (abs < 45) return 'just now'
  for (const [unit, secs] of UNITS) {
    if (abs >= secs) return RTF.format(Math.round(deltaSec / secs), unit)
  }
  return 'just now'
}

/** "2m 04s" — for elapsed timers (model loading, session runtime). */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '—'
  const s = Math.floor(seconds % 60)
  const m = Math.floor((seconds / 60) % 60)
  const h = Math.floor(seconds / 3600)
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`
  return `${s}s`
}

/** Absolute timestamp for tooltips/details; empty string on bad input. */
export function absoluteTime(value?: string | number | Date | null): string {
  if (!value) return ''
  const d = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
