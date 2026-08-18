'use client'

/**
 * ARIA - Know: research runs + background tasks
 *
 * Read-only telemetry: deep-research runs (steward/research service) and the
 * background task queue that workflow runs land in. Kept together because a
 * research run IS a background task — watching one usually means watching the
 * other.
 */
import { useResource } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type { ResearchRun, BackgroundTask } from '@/lib/api/types'
import { Card, Chip, Text, Meter } from '@/components/ui/primitives'
import { Async } from '@/components/ui/Async'
import { Stack, Columns } from '@/components/layout'
import { relativeTime } from '@/lib/time'
import { useKnowStats } from '@/features/know/knowStatus'

const RUN_TONE: Record<string, 'ok' | 'warn' | 'accent' | 'neutral'> = {
  completed: 'ok',
  running: 'accent',
  failed: 'warn',
}

export default function ResearchPage() {
  const runs = useResource<ResearchRun[]>(K.research, { tier: 'slow' })
  const tasks = useResource<BackgroundTask[]>(K.tasks, { tier: 'slow' })

  const running = (tasks.data ?? []).filter((t) => t.status === 'running' || t.status === 'pending').length

  useKnowStats([
    { label: 'RUNS', value: runs.data?.length ?? '—' },
    { label: 'TASKS', value: tasks.data?.length ?? '—' },
    ...(running > 0 ? [{ label: 'RUNNING', value: running, tone: 'ok' as const }] : []),
  ])

  return (
    <Columns lg={2}>
      <Card title="Research runs">
        <Async r={runs} skeletonRows={4} isEmpty={(d) => d.length === 0} empty="No research runs yet. Start one via Hermes or the MCP research tools.">
          {(items) => (
            <ul className="m-0 list-none p-0">
              {items.map((run) => (
                <li key={run.id} className="border-b border-line py-2 last:border-b-0">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="min-w-0 flex-1 font-sans text-prose text-ink">{run.query}</span>
                    <Chip tone={RUN_TONE[run.status ?? ''] ?? 'neutral'}>{run.status || 'unknown'}</Chip>
                    <span className="shrink-0 text-micro text-ink-faint">{relativeTime(run.created_at)}</span>
                  </div>
                  {run.progress && (
                    <p className="m-0 mt-1 text-micro text-ink-faint">
                      depth {run.progress.current_depth ?? 0}/{run.progress.max_depth ?? 0} · queries{' '}
                      {run.progress.queries_completed ?? 0}/{run.progress.queries_total ?? 0} · learnings{' '}
                      {run.progress.learnings_count ?? 0}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Async>
      </Card>

      <Card title="Background tasks" hint="workflow + system jobs, newest 10">
        <Async r={tasks} skeletonRows={4} isEmpty={(d) => d.length === 0} empty="No background tasks recorded.">
          {(items) => (
            <Stack gap="sm">
              {items.slice(0, 10).map((task) => (
                <div key={task._id} className="min-w-0">
                  <Meter
                    segments={[
                      {
                        pct: task.progress ?? 0,
                        color: task.status === 'failed' ? 'bg-gone' : task.status === 'completed' ? 'bg-live' : 'bg-accent',
                        key: 'progress',
                      },
                    ]}
                    left={task.name || task._id}
                    right={`${task.status || 'unknown'} · ${task.progress ?? 0}%`}
                    label={`${task.name}: ${task.progress ?? 0}%`}
                  />
                  {task.error && <Text clamp={2} className="mt-1 text-gone">{task.error}</Text>}
                </div>
              ))}
            </Stack>
          )}
        </Async>
      </Card>
    </Columns>
  )
}
