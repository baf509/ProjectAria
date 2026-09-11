'use client'

/**
 * ARIA - Operate: the verdict card
 *
 * The page's first answer rather than its first reading. /operate is long
 * enough that "is the fleet healthy?" was a job the operator did by hand across
 * six cards; this states the conclusion, what caused it, and what to do.
 *
 * It renders the WORST findings, not all of them — the service alarms and the
 * saturation notices below are still the actionable surfaces, and repeating
 * every one of them here would just make the summary as long as the page.
 *
 * ⚠️ It must not answer before its inputs have. A verdict computed from
 * resources still in flight reads "Fleet healthy · 0 models resident of 0
 * registered", which is an unloaded answer asserted from an absent one — the
 * same mistake the Flash Next button label was fixed for, and worse here
 * because this one claims the whole fleet is fine.
 */
import type { LlmRouteFull, ModelServerFull, ServiceFull, UtilServer } from '@/lib/api/types'
import { Card, Chip, Text } from '@/components/ui/primitives'
import { diagnose, type Verdict } from './diagnose'
import type { Machine } from './machines'

const MAX_SHOWN = 3

const TONE: Record<Verdict, { chip: 'ok' | 'warn' | 'accent' | 'neutral'; text: string; border: string }> = {
  ok: { chip: 'ok', text: 'text-live', border: 'border-live/40' },
  warn: { chip: 'warn', text: 'text-gone', border: 'border-gone/40' },
  critical: { chip: 'warn', text: 'text-gone', border: 'border-gone/50' },
  // Unobserved is not a failure and must not be painted as one — but it is not
  // a pass either, so it gets the accent rather than the live green.
  unknown: { chip: 'accent', text: 'text-accent', border: 'border-accent/40' },
}

export function Diagnosis({ ready, ...props }: {
  /** Every resource the verdict reads has answered (with data or an error). */
  ready: boolean
  machines: Machine[]
  services: ServiceFull[] | undefined
  util: UtilServer[] | undefined
  route: LlmRouteFull | undefined
  servers: ModelServerFull[] | undefined
}) {
  if (!ready) {
    return (
      <Card title="Diagnosis">
        <p className="m-0 font-mono text-num font-semibold text-ink-faint">Checking the fleet…</p>
        <Text className="mt-2">
          Reading services, model servers and hardware telemetry. No verdict until all of them answer.
        </Text>
      </Card>
    )
  }

  const d = diagnose(props)
  const tone = TONE[d.verdict]
  const shown = d.findings.slice(0, MAX_SHOWN)
  const rest = d.findings.length - shown.length

  return (
    <Card
      title="Diagnosis"
      hint={d.context.join(' · ')}
      className={tone.border}
      actions={<Chip tone={tone.chip}>{d.verdict}</Chip>}
    >
      <p className={`m-0 font-mono text-num font-semibold ${tone.text}`}>{d.headline}</p>

      {shown.length === 0 ? (
        <Text className="mt-2">
          Nothing to fix. Every always-up service is running, no backend is queuing, and no sensor
          reporting a limit is near it.
        </Text>
      ) : (
        <ul className="m-0 mt-2.5 list-none p-0">
          {shown.map((f, i) => (
            <li key={i} className="border-b border-line py-2 last:border-b-0">
              <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
                <Chip tone={TONE[f.verdict].chip}>{f.verdict}</Chip>
                <span className="min-w-0 flex-1 wrap-anywhere text-label text-ink">{f.cause}</span>
              </div>
              {f.action && <Text className="mt-1">{f.action}</Text>}
            </li>
          ))}
        </ul>
      )}

      {rest > 0 && (
        <Text className="mt-2">
          {rest} further finding{rest === 1 ? '' : 's'} below.
        </Text>
      )}
    </Card>
  )
}
