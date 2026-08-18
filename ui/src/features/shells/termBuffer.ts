/**
 * ARIA - terminal line buffer (outside React)
 *
 * The old shells page called `setEvents(prev => [...prev, evt].slice(-1500))`
 * for EVERY SSE line, re-rendering a 780-line component and the whole shell
 * list per line — at the ~480 events/s the stream replays, that is a locked
 * main thread. Here the stream writes into this plain class and React reads it
 * through `useSyncExternalStore`; notifications are coalesced to at most one
 * per animation frame, so a burst of 500 catch-up lines costs one render.
 *
 * Consecutive identical lines are collapsed into one entry with a ×count
 * (spinner redraws and blank lines dominate raw pipe-pane output), which is
 * also what keeps the DOM bounded: 1500 retained entries, not 1500 per redraw.
 */

export type TermLine = {
  /** line_number of the first event in the run — stable, so rows can be memoised. */
  key: number
  kind: string
  text: string
  count: number
  /** ts of the most recent coalesced event. */
  ts: string
}

export type TermSnapshot = {
  lines: TermLine[]
  /** Bumps once per flush; cheap dependency for scroll-to-bottom effects. */
  version: number
}

const MAX_LINES = 1500

export class TermBuffer {
  private lines: TermLine[] = []
  private snap: TermSnapshot = { lines: [], version: 0 }
  private listeners = new Set<() => void>()
  private raf: number | null = null
  /** Highest line_number seen — the resume point for a reopened stream. */
  lastLine = 0

  push(event: { line_number: number; ts: string; kind?: string; text_clean?: string }) {
    if (event.line_number <= this.lastLine) return
    this.lastLine = event.line_number
    const text = (event.text_clean ?? '').replace(/\s+$/, '')
    const kind = event.kind ?? 'output'
    const prev = this.lines[this.lines.length - 1]
    if (prev && prev.kind === kind && prev.text === text) {
      // Replace rather than mutate: memoised rows compare by object identity,
      // so an in-place `count++` would never repaint the ×count badge.
      this.lines[this.lines.length - 1] = { ...prev, count: prev.count + 1, ts: event.ts }
    } else {
      this.lines.push({ key: event.line_number, kind, text, count: 1, ts: event.ts })
      if (this.lines.length > MAX_LINES) this.lines.splice(0, this.lines.length - MAX_LINES)
    }
    this.schedule()
  }

  reset() {
    this.lines = []
    this.lastLine = 0
    this.schedule()
  }

  private schedule() {
    if (this.raf !== null) return
    const flush = () => {
      this.raf = null
      this.snap = { lines: [...this.lines], version: this.snap.version + 1 }
      for (const l of Array.from(this.listeners)) l()
    }
    this.raf =
      typeof requestAnimationFrame === 'function'
        ? requestAnimationFrame(flush)
        : (setTimeout(flush, 16) as unknown as number)
  }

  subscribe = (cb: () => void) => {
    this.listeners.add(cb)
    return () => {
      this.listeners.delete(cb)
    }
  }

  getSnapshot = (): TermSnapshot => this.snap
}
