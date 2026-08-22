/**
 * ARIA - number and identifier formatting
 */

export function gib(value?: number | null, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value.toFixed(digits)} GiB`
}

export function count(value?: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value.toLocaleString()
}

export function usd(value?: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value < 0.01 && value > 0 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`
}

export function pct(value?: number | null, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

/**
 * Machine slugs discriminate at BOTH ends
 * (`DS4-0731-Q8Protected-Halo-DwarfStar` vs `DS4-0731-IQ3_S-Hybrid-ROCm-Dual`), so
 * a trailing ellipsis destroys exactly the part that identifies them. Middle
 * truncation keeps both.
 */
export function middleTruncate(text: string, max = 28): string {
  if (text.length <= max) return text
  const keep = max - 1
  const head = Math.ceil(keep / 2)
  const tail = Math.floor(keep / 2)
  return `${text.slice(0, head)}…${text.slice(text.length - tail)}`
}
