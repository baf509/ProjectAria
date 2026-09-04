'use client'

/**
 * ARIA - Know: usage (tokens + cost by model and agent)
 *
 * Three small aggregates over `usage_records`. Local backends cost $0, so the
 * cost column mattering at all means a cloud backend was used — it is kept
 * visible for exactly that reason (the spend circuit-breaker trips on hourly
 * priced spend). Tables live inside ScrollX: a 7-column table is the one
 * legitimate wide element here and must scroll in its own box, never widen
 * the page.
 */
import { useResource } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type { UsageSummary, UsageRow } from '@/lib/api/types'
import { Card, KeyValue } from '@/components/ui/primitives'
import { Async } from '@/components/ui/Async'
import { Stack, ScrollX } from '@/components/layout'
import { count, usd, pct, middleTruncate } from '@/lib/format'
import { useKnowStats } from '@/features/know/knowStatus'

const DAYS = 7

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

export default function UsagePage() {
  const summary = useResource<UsageSummary>(K.usage(DAYS), { tier: 'lazy' })
  const byModel = useResource<UsageRow[]>(K.usageByModel(DAYS), { tier: 'lazy' })
  const byAgent = useResource<UsageRow[]>(K.usageByAgent(DAYS), { tier: 'lazy' })
  const byCaller = useResource<UsageRow[]>(K.usageByCaller(DAYS), { tier: 'lazy' })

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
    </Stack>
  )
}
