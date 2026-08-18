/**
 * ARIA - Benchmarks: shared bits for the list and detail routes.
 *
 * The status→tone map is total over BenchRunStatus, which now includes
 * `interrupted` (registry reaper marks a run whose pid died with the API);
 * the old page's Record omitted it and an interrupted run rendered with
 * `undefined` as its className.
 */
import type { BenchRun, BenchRunStatus } from '@/lib/api/types'
import { formatDuration } from '@/lib/time'

export const RUN_TONE: Record<BenchRunStatus, 'accent' | 'ok' | 'warn' | 'neutral'> = {
  running: 'accent',
  succeeded: 'ok',
  failed: 'warn',
  interrupted: 'warn',
  cancelled: 'neutral',
  unknown: 'neutral',
}

/** Elapsed for finished runs, still-ticking for running ones. Epoch SECONDS. */
export function runDuration(run: BenchRun): string {
  const end = run.finished_at ?? Date.now() / 1000
  return formatDuration(Math.max(0, end - run.started_at))
}

/** started_at is epoch seconds; relativeTime expects ms. */
export const startedMs = (run: BenchRun) => run.started_at * 1000
