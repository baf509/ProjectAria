'use client'

/**
 * ARIA - Know: usage (tokens + cost by model and agent)
 *
 * Aggregate cost/cache views plus content-free per-request inference traces.
 * Local backends cost $0, so a nonzero cost means a cloud backend was used.
 * Wide tables live inside ScrollX and must scroll in their own box, never
 * widen the page.
 */
import { useResource } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type { InferenceTrace, UsageSummary, UsageRow } from '@/lib/api/types'
import { Card, KeyValue } from '@/components/ui/primitives'
import { Async } from '@/components/ui/Async'
import { Stack, ScrollX } from '@/components/layout'
import { count, usd, pct, middleTruncate } from '@/lib/format'
import { useKnowStats } from '@/features/know/knowStatus'

const DAYS = 7

function traceTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString(undefined, { hour12: false })
}

function milliseconds(value?: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value < 1000 ? `${Math.round(value)}ms` : `${(value / 1000).toFixed(1)}s`
}

function prefixState(trace: InferenceTrace): string {
  const state = trace.preamble?.state
  if (state === 'changed') return trace.preamble?.change_reason?.replaceAll('_', ' ') || 'changed'
  if (state === 'first_seen') return 'first seen'
  return state || 'absent'
}

function UsageTable({ rows, nameLabel }: { rows: UsageRow[]; nameLabel: string }) {
  return (
    <ScrollX>
      <table className="w-full min-w-[42rem] border-collapse text-label">
        <thead>
          <tr className="border-b border-line text-left text-micro uppercase tracking-[0.08em] text-ink-faint">
            <th className="whitespace-nowrap py-1.5 pr-3 font-medium">{nameLabel}</th>
            <th className="whitespace-nowrap py-1.5 pr-3 font-medium">Backend</th>
            <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">Requests</th>
            <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">In</th>
            <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">Out</th>
            <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">Total</th>
            <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">Cache</th>
            <th className="whitespace-nowrap py-1.5 text-right font-medium">Cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={row._id ?? `row-${i}`} className="border-b border-line last:border-b-0">
              <td className="py-1.5 pr-3 font-mono text-micro text-ink" title={row._id ?? undefined}>
                {middleTruncate(row._id || 'unknown', 36)}
              </td>
              <td className="py-1.5 pr-3 text-ink-dim">{row.backend || '—'}</td>
              <td className="tnum py-1.5 pr-3 text-right">{count(row.requests)}</td>
              <td className="tnum py-1.5 pr-3 text-right">{count(row.input_tokens)}</td>
              <td className="tnum py-1.5 pr-3 text-right">{count(row.output_tokens)}</td>
              <td className="tnum py-1.5 pr-3 text-right">{count(row.total_tokens)}</td>
              <td className="tnum py-1.5 pr-3 text-right">
                {row.cache_hit_rate !== undefined ? pct(row.cache_hit_rate) : '—'}
              </td>
              <td className="tnum py-1.5 text-right">{row.cost !== undefined ? usd(row.cost) : '$0.00'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </ScrollX>
  )
}

function TraceTable({ rows }: { rows: InferenceTrace[] }) {
  return (
    <ScrollX>
      <table className="w-full min-w-[64rem] border-collapse text-label">
        <thead>
          <tr className="border-b border-line text-left text-micro uppercase tracking-[0.08em] text-ink-faint">
            <th className="whitespace-nowrap py-1.5 pr-3 font-medium">Time</th>
            <th className="whitespace-nowrap py-1.5 pr-3 font-medium">Caller</th>
            <th className="whitespace-nowrap py-1.5 pr-3 font-medium">Model</th>
            <th className="whitespace-nowrap py-1.5 pr-3 font-medium">Result</th>
            <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">Context</th>
            <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">Cache</th>
            <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">MTP</th>
            <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">Queue</th>
            <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">Decode</th>
            <th className="whitespace-nowrap py-1.5 font-medium">Prefix</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((trace, i) => (
            <tr
              key={trace.trace_id ?? `trace-${i}`}
              className="border-b border-line last:border-b-0"
              title={[
                trace.trace_id ? `trace ${trace.trace_id}` : null,
                `total ${milliseconds(trace.latency_ms)}`,
                `route ${milliseconds(trace.routing_ms)}`,
                `backend ${milliseconds(trace.backend_ms)}`,
                trace.first_chunk_ms != null ? `first chunk ${milliseconds(trace.first_chunk_ms)}` : null,
              ].filter(Boolean).join(' · ')}
            >
              <td className="tnum whitespace-nowrap py-1.5 pr-3 text-micro text-ink-dim">{traceTime(trace.timestamp)}</td>
              <td className="py-1.5 pr-3 font-mono text-micro text-ink" title={trace.caller ?? undefined}>
                {middleTruncate(trace.caller || 'unknown', 24)}
              </td>
              <td className="py-1.5 pr-3 font-mono text-micro text-ink-dim" title={trace.model ?? undefined}>
                {middleTruncate(trace.model || 'unknown', 28)}
              </td>
              <td className="py-1.5 pr-3 text-ink-dim">{trace.outcome || trace.status_code || '—'}</td>
              <td className="tnum py-1.5 pr-3 text-right">{count(trace.context_tokens)}</td>
              <td className="tnum py-1.5 pr-3 text-right">{pct(trace.cache_hit_rate)}</td>
              <td className="tnum py-1.5 pr-3 text-right">{pct(trace.speculative_acceptance_rate)}</td>
              <td className="tnum py-1.5 pr-3 text-right">{milliseconds(trace.queue_wait_ms)}</td>
              <td className="tnum py-1.5 pr-3 text-right">
                {trace.decode_tokens_per_second != null ? `${trace.decode_tokens_per_second.toFixed(1)} t/s` : '—'}
              </td>
              <td className="py-1.5 text-micro text-ink-dim" title={trace.preamble?.fingerprint ?? undefined}>
                {prefixState(trace)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </ScrollX>
  )
}

export default function UsagePage() {
  const summary = useResource<UsageSummary>(K.usage(DAYS), { tier: 'lazy' })
  const byModel = useResource<UsageRow[]>(K.usageByModel(DAYS), { tier: 'lazy' })
  const byAgent = useResource<UsageRow[]>(K.usageByAgent(DAYS), { tier: 'lazy' })
  const byCaller = useResource<UsageRow[]>(K.usageByCaller(DAYS), { tier: 'lazy' })
  const traces = useResource<InferenceTrace[]>(K.usageTraces(24, 50), { tier: 'lazy' })

  const totalCost = (byModel.data ?? []).reduce((acc, r) => acc + (r.cost ?? 0), 0)

  useKnowStats([
    { label: 'REQUESTS', value: count(summary.data?.requests) },
    { label: 'TOKENS', value: count(summary.data?.total_tokens) },
    { label: 'COST', value: usd(totalCost), tone: totalCost > 0 ? 'warn' : 'default' },
  ])

  return (
    <Stack>
      <Card title={`Summary · last ${DAYS} days`}>
        <Async r={summary} skeletonRows={3}>
          {(d) => (
            <KeyValue
              items={[
                { k: 'Requests', v: count(d.requests), kind: 'num' },
                { k: 'Input tokens', v: count(d.input_tokens), kind: 'num' },
                { k: 'Output tokens', v: count(d.output_tokens), kind: 'num' },
                { k: 'Total tokens', v: count(d.total_tokens), kind: 'num' },
                { k: 'Cache read', v: count(d.cache_read_tokens), kind: 'num' },
                { k: 'Cache hit rate', v: pct(d.cache_hit_rate), kind: 'num' },
              ]}
            />
          )}
        </Async>
      </Card>

      <Card title="By model">
        <Async r={byModel} skeletonRows={3} isEmpty={(d) => d.length === 0} empty="No usage recorded in this window.">
          {(rows) => <UsageTable rows={rows} nameLabel="Model" />}
        </Async>
      </Card>

      <Card title="By agent">
        <Async r={byAgent} skeletonRows={3} isEmpty={(d) => d.length === 0} empty="No usage recorded in this window.">
          {(rows) => <UsageTable rows={rows} nameLabel="Agent" />}
        </Async>
      </Card>

      <Card title="By gateway caller">
        <Async r={byCaller} skeletonRows={3} isEmpty={(d) => d.length === 0} empty="No attributed gateway usage in this window.">
          {(rows) => <UsageTable rows={rows} nameLabel="Caller" />}
        </Async>
      </Card>

      <Card title="Recent inference traces · last 24 hours">
        <Async r={traces} skeletonRows={5} isEmpty={(d) => d.length === 0} empty="No gateway traces recorded in this window.">
          {(rows) => <TraceTable rows={rows} />}
        </Async>
      </Card>
    </Stack>
  )
}
