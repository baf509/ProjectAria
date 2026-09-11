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
import type { InferenceTrace, UsageSummary, UsageRow, UsageSeries } from '@/lib/api/types'
import { Card, KeyValue, Text } from '@/components/ui/primitives'
import { Async } from '@/components/ui/Async'
import { Stack, ScrollX, Grid } from '@/components/layout'
import { count, usd, pct, middleTruncate } from '@/lib/format'
import { useKnowStats } from '@/features/know/knowStatus'
import {
  ChartLegend,
  RankedBars,
  StackedBars,
  TrendLine,
  type StackedBucket,
} from '@/components/ui/charts'

const DAYS = 7
/** The charts read a wider window than the tables: a week of daily bars is a
    week of bars, and the shape only starts to mean anything past that. */
const CHART_DAYS = 30
const TREND_HOURS = 72
const TOP_CALLERS = 10

/** Compact token counts for an axis. Full precision lives in the tooltip. */
function tokensAxis(v: number): string {
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`
  if (v >= 1e6) return `${(v / 1e6).toFixed(v >= 1e7 ? 0 : 1)}M`
  if (v >= 1e3) return `${Math.round(v / 1e3)}k`
  return String(Math.round(v))
}

/* Buckets are $dateTrunc'd in UTC, so they must be LABELLED in UTC. Formatting
   them locally slides every bucket by the offset — a UTC-midnight day bucket
   renders as the previous day's date in any western timezone. */
function dayLabel(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? iso.slice(5, 10)
    : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
}

function hourLabel(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? iso
    : `${d.toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', hour12: false, timeZone: 'UTC',
      })} UTC`
}

/**
 * Put the absent buckets back, so the x axis is time rather than a list.
 *
 * The API returns only buckets that contain requests — it cannot know whether a
 * hole is a zero or a gap, so it declines to guess. Here we know: for TOKENS a
 * quiet day is a real zero and belongs on the axis, and without it two dates a
 * fortnight apart render side by side as though they were consecutive.
 *
 * The measured fields are carried through untouched; a filled bucket has no
 * cache rate at all, which keeps it a gap in the line rather than a floor.
 */
function densify<B extends { t: string }>(
  buckets: B[],
  { end, count, stepMs }: { end: number; count: number; stepMs: number }
): { t: string; found?: B }[] {
  const bySlot = new Map(buckets.map((b) => [Math.floor(Date.parse(b.t) / stepMs), b]))
  const last = Math.floor(end / stepMs)
  const out: { t: string; found?: B }[] = []
  for (let i = count - 1; i >= 0; i--) {
    const slot = last - i
    out.push({ t: new Date(slot * stepMs).toISOString(), found: bySlot.get(slot) })
  }
  return out
}

const HOUR_MS = 3_600_000
const DAY_MS = 86_400_000

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
                {/* A backend that never reports reuse is not a backend with no
                    reuse: Red's Radiance returns a null prompt_tokens_details,
                    and printing 0% there described a working prefix cache as
                    absent. Say "not reported" and mean it. */}
                {row.cache_reporting === 'unsupported' ? (
                  <span className="text-ink-faint" title="This backend does not report prompt-cache reuse. Its cache may still be working.">
                    not reported
                  </span>
                ) : (
                  <>
                    {pct(row.cache_hit_rate)}
                    {row.cache_reporting === 'partial' && (
                      <span className="text-ink-faint" title="Some requests in this group came from a backend that does not report reuse; the rate covers only those that do.">
                        *
                      </span>
                    )}
                  </>
                )}
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
              <td className="tnum py-1.5 pr-3 text-right">
                {trace.cache_reported === false ? (
                  <span className="text-ink-faint" title="This backend does not report prompt-cache reuse.">n/r</span>
                ) : (
                  pct(trace.cache_hit_rate)
                )}
              </td>
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

/**
 * Tokens per day, split by model.
 *
 * The rotation is the thing a total-sorted table structurally cannot show:
 * which model was actually carrying load, and when.
 */
function TokensByModel({ r }: { r: ReturnType<typeof useResource<UsageSeries>> }) {
  return (
    <Card title={`Tokens by model · last ${CHART_DAYS} days`} hint="daily totals">
      <Async r={r} skeletonRows={6} isEmpty={(d) => d.buckets.length === 0} empty="No usage recorded in this window.">
        {(d) => {
          const buckets: StackedBucket[] = densify(d.buckets, {
            end: Date.now(), count: CHART_DAYS, stepMs: DAY_MS,
          }).map(({ t, found }) => ({
            label: dayLabel(t),
            parts: found?.by ?? {},
            // A day with no requests really did spend no tokens.
            total: found?.total_tokens ?? 0,
          }))
          return (
            <>
              <StackedBars
                buckets={buckets}
                series={d.series}
                format={tokensAxis}
                ariaLabel={`Tokens per day by model over the last ${CHART_DAYS} days`}
              />
              <ChartLegend names={d.series} />
            </>
          )
        }}
      </Async>
    </Card>
  )
}

/**
 * Prompt-cache reuse over time.
 *
 * A bucket served only by backends that do not report reuse arrives as `null`
 * and is drawn as a GAP with a baseline tick. Putting it on the floor would
 * restate the exact bug `cache_reporting` exists to prevent — Red's Radiance
 * returns a null prompt_tokens_details, and 33M prompt tokens on a measurably
 * working prefix cache once read as no reuse at all.
 */
function CacheTrend({ r }: { r: ReturnType<typeof useResource<UsageSeries>> }) {
  return (
    <Card title={`Prompt-cache hit rate · last ${TREND_HOURS} hours`} hint="hourly">
      <Async r={r} skeletonRows={5} isEmpty={(d) => d.buckets.length < 2} empty="Not enough history in this window.">
        {(d) => {
          const slots = densify(d.buckets, { end: Date.now(), count: TREND_HOURS, stepMs: HOUR_MS })
          // Two different reasons a line breaks, and they mean different
          // things: nobody asked, versus nobody answered. Neither is 0% reuse.
          const idle = slots.filter((s) => !s.found).length
          const unreported = slots.filter((s) => s.found && s.found.cache_hit_rate == null).length
          return (
            <>
              <TrendLine
                points={slots.map(({ t, found }) => ({
                  label: hourLabel(t),
                  value: found?.cache_hit_rate ?? null,
                }))}
                domain={[0, 1]}
                format={(v) => pct(v)}
                ariaLabel={`Hourly prompt-cache hit rate over the last ${TREND_HOURS} hours`}
              />
              <Text className="mt-2.5">
                A tick on the baseline marks an hour with no rate to plot
                {unreported > 0 || idle > 0
                  ? ` — ${[
                      unreported > 0 ? `${unreported} whose backends did not report reuse` : null,
                      idle > 0 ? `${idle} with no requests at all` : null,
                    ].filter(Boolean).join(', ')}, of ${TREND_HOURS}`
                  : ''}. Neither is an hour that reused nothing, so the line breaks rather than
                touching zero.
              </Text>
            </>
          )
        }}
      </Async>
    </Card>
  )
}

/** Who spent the tokens. Ten bars carry the shape; the table below has all of them. */
function CallerRanking({ r }: { r: ReturnType<typeof useResource<UsageRow[]>> }) {
  return (
    <Card title="Top gateway callers" hint={`by tokens · last ${DAYS} days`}>
      <Async r={r} skeletonRows={5} isEmpty={(d) => d.length === 0} empty="No attributed gateway usage in this window.">
        {(rows) => {
          const top = rows.slice(0, TOP_CALLERS)
          return (
            <>
              <RankedBars
                rows={top.map((row) => ({
                  name: row._id || 'unknown',
                  value: row.total_tokens ?? 0,
                  note: `${count(row.requests)} requests`,
                }))}
                format={tokensAxis}
                ariaLabel={`Top ${top.length} gateway callers by tokens`}
              />
              {rows.length > top.length && (
                <Text className="mt-2.5">
                  {rows.length - top.length} further callers are in the table below.
                </Text>
              )}
            </>
          )
        }}
      </Async>
    </Card>
  )
}

export default function UsagePage() {
  const summary = useResource<UsageSummary>(K.usage(DAYS), { tier: 'lazy' })
  const byModel = useResource<UsageRow[]>(K.usageByModel(DAYS), { tier: 'lazy' })
  const byAgent = useResource<UsageRow[]>(K.usageByAgent(DAYS), { tier: 'lazy' })
  const byCaller = useResource<UsageRow[]>(K.usageByCaller(DAYS), { tier: 'lazy' })
  const traces = useResource<InferenceTrace[]>(K.usageTraces(24, 50), { tier: 'lazy' })
  const daily = useResource<UsageSeries>(K.usageSeries(CHART_DAYS, 'day', 'model', 4), { tier: 'lazy' })
  const hourly = useResource<UsageSeries>(
    K.usageSeries(Math.ceil(TREND_HOURS / 24), 'hour', 'none'),
    { tier: 'lazy' }
  )

  const totalCost = (byModel.data ?? []).reduce((acc, r) => acc + (r.cost ?? 0), 0)

  useKnowStats([
    { label: 'REQUESTS', value: count(summary.data?.requests) },
    { label: 'TOKENS', value: count(summary.data?.total_tokens) },
    { label: 'COST', value: usd(totalCost), tone: totalCost > 0 ? 'warn' : 'default' },
  ])

  return (
    <Stack>
      <TokensByModel r={daily} />

      <Grid>
        <CacheTrend r={hourly} />
        <CallerRanking r={byCaller} />
      </Grid>

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
                {
                  k: 'Cache hit rate',
                  v: d.cache_reporting === 'unsupported' ? 'not reported' : pct(d.cache_hit_rate),
                  kind: 'num',
                },
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
