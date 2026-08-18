'use client'

/**
 * ARIA - Benchmark launch form
 *
 * Two things this form must be honest about, because they have real
 * consequences:
 *   1. A run STOPS AND STARTS MODEL SERVERS. Only one run may be in flight,
 *      and a run that would disturb a server bound to an agent comes back as a
 *      409 {error, conflicts} — surfaced with an explicit "Run anyway" that
 *      retries with force, never silently.
 *   2. Runs take minutes to hours; nothing here is synchronous.
 *
 * Suites/targets are 44px toggle rows rather than the old bare
 * `<input type=checkbox>` labels (measured 2026-08-17: every checkbox on this
 * page was a sub-20px target, and the row text sat at 10-11px).
 */
import { useMemo, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useAction } from '@/lib/swr'
import { startBenchRun } from '@/lib/api/endpoints'
import type { BenchConflictDetail, BenchSuite, BenchTarget } from '@/lib/api/types'
import { Notice } from '@/components/ui/primitives'
import { Button, Field, Input } from '@/components/ui/controls'
import { Stack } from '@/components/layout'

const cx = (...p: Array<string | false | undefined>) => p.filter(Boolean).join(' ')

/**
 * A full-width selectable row at control height. Local because controls.tsx
 * has no checkbox/toggle primitive yet (noted for a shared-components pass);
 * it copies Button's focus/touch classes so the 44px guarantee still holds.
 */
function ToggleRow({
  checked,
  onToggle,
  label,
  detail,
}: {
  checked: boolean
  onToggle: () => void
  label: string
  detail?: string
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      onClick={onToggle}
      className={cx(
        'flex min-h-control w-full min-w-0 items-center gap-2.5 rounded-sm border px-2.5 text-left transition-colors',
        'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent [touch-action:manipulation]',
        checked ? 'border-accent bg-accent/10' : 'border-line bg-transparent hover:border-ink-faint'
      )}
    >
      <span
        aria-hidden="true"
        className={cx(
          'grid h-4 w-4 shrink-0 place-items-center rounded-sm border text-micro',
          checked ? 'border-accent bg-accent text-accent-ink' : 'border-ink-faint'
        )}
      >
        {checked ? '✓' : ''}
      </span>
      <span className="min-w-0 flex-1 py-1.5">
        <span className={cx('block break-all font-mono text-body', checked ? 'text-accent' : 'text-ink')}>{label}</span>
        {detail && <span className="block wrap-anywhere text-micro text-ink-faint">{detail}</span>}
      </span>
    </button>
  )
}

export function LaunchForm({
  suites,
  targets,
  budget,
  runInFlight,
  onError,
}: {
  suites: BenchSuite[]
  targets: BenchTarget[]
  budget: number
  /** A run is already running — the API allows one at a time. */
  runInFlight: boolean
  onError: (text: string) => void
}) {
  const router = useRouter()
  const run = useAction()
  const [selectedSuites, setSelectedSuites] = useState<string[]>(['performance'])
  const [selectedTargets, setSelectedTargets] = useState<string[]>([])
  const [limit, setLimit] = useState('')
  const [busy, setBusy] = useState(false)
  const [conflicts, setConflicts] = useState<string[] | null>(null)

  const toggle = (arr: string[], v: string, set: (x: string[]) => void) =>
    set(arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v])

  // Largest selected local model: the harness refuses anything over the GPU
  // budget, so warn before the round-trip.
  const estVram = useMemo(
    () =>
      selectedTargets
        .map((n) => targets.find((t) => t.name === n))
        .filter((t): t is BenchTarget => !!t && !t.cloud)
        .reduce((mx, t) => Math.max(mx, t.vram_gb ?? 0), 0),
    [selectedTargets, targets]
  )

  async function launch(force = false) {
    setBusy(true)
    setConflicts(null)
    const started = await run(
      () =>
        startBenchRun({
          suites: selectedSuites,
          targets: selectedTargets,
          limit: limit ? Number(limit) : undefined,
          force,
        }),
      {
        invalidate: ['/benchmarks'],
        onError: (e) => {
          const d = e.detail as BenchConflictDetail | undefined
          if (e.status === 409 && d && typeof d === 'object' && Array.isArray(d.conflicts)) {
            setConflicts(d.conflicts)
          } else {
            onError(e.message)
          }
        },
      }
    )
    setBusy(false)
    // The run is the selection: navigate to its route so reload/Back keep it.
    if (started) router.push(`/operate/benchmarks/${encodeURIComponent(started.run_id)}`)
  }

  return (
    <Stack gap="sm">
      <p className="m-0 text-micro uppercase tracking-[0.1em] text-ink-faint">Suites</p>
      <div className="flex min-w-0 flex-col gap-1.5">
        {suites.map((s) => (
          <ToggleRow
            key={s.name}
            checked={selectedSuites.includes(s.name)}
            onToggle={() => toggle(selectedSuites, s.name, setSelectedSuites)}
            label={s.name}
            detail={s.benches.map((b) => b.id).join(', ')}
          />
        ))}
      </div>

      <p className="m-0 mt-2 text-micro uppercase tracking-[0.1em] text-ink-faint">Targets</p>
      <div className="flex min-w-0 flex-col gap-1.5">
        {targets.map((t) => (
          <ToggleRow
            key={t.name}
            checked={selectedTargets.includes(t.name)}
            onToggle={() => toggle(selectedTargets, t.name, setSelectedTargets)}
            label={t.name}
            detail={t.cloud ? 'cloud' : `${t.vram_gb ?? 0} GB`}
          />
        ))}
      </div>
      {estVram > budget && budget > 0 && (
        <Notice tone="warn">
          Largest selected model is {estVram} GB, over the {budget} GB budget — the harness will refuse it.
        </Notice>
      )}

      <Field label="Limit samples per benchmark" hint="Leave empty for a full run. Full runs can take hours.">
        <Input
          value={limit}
          inputMode="numeric"
          onChange={(e) => setLimit(e.target.value.replace(/\D/g, ''))}
          placeholder="all"
          // globals.css raises inputs to 16px under pointer:coarse (iOS
          // focus-zoom), but that rule lives in @layer base and loses to the
          // `text-body` utility inside the Input primitive — measured 14px on
          // touch. Inline style outranks both; fix belongs in controls.tsx
          // (noted as a shared change).
          style={{ fontSize: 'max(1rem, var(--fs-body))' }}
        />
      </Field>

      <Notice>Running a benchmark stops and starts model servers. Only one run at a time.</Notice>

      <Button
        variant="primary"
        busy={busy}
        disabled={runInFlight || !selectedSuites.length || !selectedTargets.length}
        onClick={() => launch(false)}
        className="w-full"
      >
        {runInFlight ? 'A run is already in progress' : 'Start benchmark'}
      </Button>

      {conflicts && (
        <Notice tone="warn">
          <Stack gap="sm">
            <span>This would disturb model server(s) currently bound to an agent:</span>
            <ul className="m-0 list-inside list-disc p-0">
              {conflicts.map((c) => (
                <li key={c} className="wrap-anywhere font-mono text-label">
                  {c}
                </li>
              ))}
            </ul>
            <Button variant="danger" busy={busy} onClick={() => launch(true)} className="self-start">
              Run anyway
            </Button>
          </Stack>
        </Notice>
      )}
    </Stack>
  )
}
