'use client'

// ARIA - Benchmarks cockpit.
//
// Pick suites (code / tool-use / performance / agents) + targets, launch an
// evalstack run, watch it, and read the resulting leaderboard.
//
// Two things this UI must be honest about, because they have real consequences:
//   1. A run STOPS AND STARTS MODEL SERVERS. That is why only one run may be in
//      flight, and why a run that would disturb a bound model needs confirming.
//   2. Runs take minutes to hours. Nothing here is synchronous; we poll.

import { useCallback, useEffect, useMemo, useState } from 'react'
import { AppShell } from '@/components/AppShell'
import {
  benchmarksApi,
  BoundConflictError,
  type BenchRun,
  type BenchTarget,
  type Suite,
} from '@/lib/api-client-benchmarks'

function relTime(epochSeconds: number | null): string {
  if (!epochSeconds) return '—'
  const delta = Math.floor(Date.now() / 1000 - epochSeconds)
  if (delta < 60) return `${delta}s ago`
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`
  return `${Math.floor(delta / 86400)}d ago`
}

function duration(run: BenchRun): string {
  const end = run.finished_at ?? Date.now() / 1000
  const secs = Math.max(0, Math.floor(end - run.started_at))
  if (secs < 60) return `${secs}s`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`
}

const STATUS_STYLES: Record<BenchRun['status'], string> = {
  running: 'bg-accent/15 text-accent border-accent/30',
  succeeded: 'bg-live/15 text-live border-live/30',
  failed: 'bg-gone/15 text-gone border-gone/30',
  cancelled: 'bg-panel-2/15 text-ink-dim border-line/30',
  unknown: 'bg-panel-2/15 text-ink-dim border-line/30',
}

export default function BenchmarksPage() {
  const [suites, setSuites] = useState<Suite[]>([])
  const [targets, setTargets] = useState<BenchTarget[]>([])
  const [budget, setBudget] = useState<number>(0)
  const [runs, setRuns] = useState<BenchRun[]>([])
  const [selectedSuites, setSelectedSuites] = useState<string[]>(['performance'])
  const [selectedTargets, setSelectedTargets] = useState<string[]>([])
  const [limit, setLimit] = useState<string>('')
  const [active, setActive] = useState<BenchRun | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [conflicts, setConflicts] = useState<string[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [unavailable, setUnavailable] = useState<string | null>(null)

  const running = useMemo(() => runs.find((r) => r.status === 'running') ?? null, [runs])

  const loadStatic = useCallback(async () => {
    try {
      const h = await benchmarksApi.health()
      if (!h.available) {
        setUnavailable(`evalstack not found at ${h.root}`)
        return
      }
      const [s, t] = await Promise.all([benchmarksApi.suites(), benchmarksApi.targets()])
      setSuites(s)
      setTargets(t.targets)
      setBudget(t.gpu_budget_gb)
    } catch (e) {
      setUnavailable(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const loadRuns = useCallback(async () => {
    try {
      setRuns(await benchmarksApi.listRuns())
    } catch {
      /* transient; the poll will retry */
    }
  }, [])

  useEffect(() => {
    loadStatic()
    loadRuns()
  }, [loadStatic, loadRuns])

  // Poll while something is in flight so the log tail and metrics stay live.
  useEffect(() => {
    if (!running && !active) return
    const id = setInterval(async () => {
      await loadRuns()
      if (active) {
        try {
          setActive(await benchmarksApi.getRun(active.run_id))
        } catch {
          /* keep the last good view */
        }
      }
    }, 5000)
    return () => clearInterval(id)
  }, [running, active, loadRuns])

  const toggle = (arr: string[], v: string, set: (x: string[]) => void) =>
    set(arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v])

  const estVram = useMemo(
    () =>
      selectedTargets
        .map((n) => targets.find((t) => t.name === n))
        .filter((t): t is BenchTarget => !!t && !t.cloud)
        .reduce((mx, t) => Math.max(mx, t.vram_gb), 0),
    [selectedTargets, targets],
  )

  const launch = async (force = false) => {
    setError(null)
    setConflicts(null)
    setBusy(true)
    try {
      const run = await benchmarksApi.startRun({
        suites: selectedSuites,
        targets: selectedTargets,
        limit: limit ? Number(limit) : undefined,
        force,
      })
      setActive(run)
      await loadRuns()
    } catch (e) {
      if (e instanceof BoundConflictError) setConflicts(e.conflicts)
      else setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (unavailable) {
    return (
      <AppShell area="Operate">
        <h1 className="text-xl font-semibold">Benchmarks</h1>
        <p className="mt-4 rounded border border-gone/30 bg-gone/10 p-4 text-sm text-gone">
          Benchmark harness unavailable: {unavailable}
        </p>
      </AppShell>
    )
  }

  return (
    <AppShell area="Operate">
      <div className="mb-6 flex items-baseline gap-4">
        <a href="/dashboard" className="text-ink-dim hover:text-ink">
          ← dashboard
        </a>
        <h1 className="text-xl font-semibold">Benchmarks</h1>
        <span className="text-xs text-ink-faint">GPU budget {budget} GB</span>
      </div>

      <div className="grid gap-6 lg:grid-cols-[380px_1fr]">
        {/* ------------------------------------------------ launch form ---- */}
        <section className="space-y-4 rounded-lg border border-line bg-panel/40 p-4">
          <div>
            <h2 className="mb-2 text-sm font-semibold text-ink-dim">Suites</h2>
            <div className="space-y-1">
              {suites.map((s) => (
                <label key={s.name} className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={selectedSuites.includes(s.name)}
                    onChange={() => toggle(selectedSuites, s.name, setSelectedSuites)}
                  />
                  <span>
                    <span className="font-medium">{s.name}</span>
                    <span className="ml-2 text-xs text-ink-faint">
                      {s.benches.map((b) => b.id).join(', ')}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </div>

          <div>
            <h2 className="mb-2 text-sm font-semibold text-ink-dim">Targets</h2>
            <div className="space-y-1">
              {targets.map((t) => (
                <label key={t.name} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={selectedTargets.includes(t.name)}
                    onChange={() => toggle(selectedTargets, t.name, setSelectedTargets)}
                  />
                  <span className="font-medium">{t.name}</span>
                  <span className="text-xs text-ink-faint">
                    {t.cloud ? 'cloud' : `${t.vram_gb} GB`}
                  </span>
                </label>
              ))}
            </div>
            {estVram > budget && (
              <p className="mt-2 text-xs text-accent">
                Largest selected model is {estVram} GB, over the {budget} GB budget — the
                harness will refuse it.
              </p>
            )}
          </div>

          <div>
            <label className="text-sm text-ink-dim">
              Limit samples per benchmark{' '}
              <input
                value={limit}
                onChange={(e) => setLimit(e.target.value.replace(/\D/g, ''))}
                placeholder="all"
                className="ml-2 w-20 rounded border border-line bg-ground px-2 py-1 text-sm"
              />
            </label>
            <p className="mt-1 text-xs text-ink-faint">
              Leave empty for a full run. Full runs can take hours.
            </p>
          </div>

          <p className="rounded border border-accent/30 bg-accent/10 p-2 text-xs text-accent">
            Running a benchmark stops and starts model servers. Only one run at a time.
          </p>

          <button
            disabled={busy || !!running || !selectedSuites.length || !selectedTargets.length}
            onClick={() => launch(false)}
            className="w-full rounded bg-live px-3 py-2 text-sm font-medium text-ink disabled:cursor-not-allowed disabled:bg-panel-2"
          >
            {running ? 'A run is already in progress' : busy ? 'Starting…' : 'Start benchmark'}
          </button>

          {conflicts && (
            <div className="rounded border border-accent/40 bg-accent/10 p-3 text-xs">
              <p className="mb-2 text-accent">
                This would disturb model server(s) currently bound to an agent:
              </p>
              <ul className="mb-2 list-inside list-disc text-accent">
                {conflicts.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
              <button
                onClick={() => launch(true)}
                className="rounded bg-accent px-2 py-1 font-medium text-ink"
              >
                Run anyway
              </button>
            </div>
          )}
          {error && (
            <p className="rounded border border-gone/30 bg-gone/10 p-2 text-xs text-gone">
              {error}
            </p>
          )}
        </section>

        {/* ----------------------------------------------------- results ---- */}
        <section className="space-y-4">
          <div className="rounded-lg border border-line bg-panel/40 p-4">
            <h2 className="mb-3 text-sm font-semibold text-ink-dim">Runs</h2>
            {!runs.length && <p className="text-sm text-ink-faint">No runs yet.</p>}
            <div className="space-y-1">
              {runs.map((r) => (
                <button
                  key={r.run_id}
                  onClick={async () => setActive(await benchmarksApi.getRun(r.run_id))}
                  className={`flex w-full items-center gap-3 rounded px-2 py-1.5 text-left text-sm hover:bg-panel-2/60 ${
                    active?.run_id === r.run_id ? 'bg-panel-2/60' : ''
                  }`}
                >
                  <span className={`rounded border px-1.5 py-0.5 text-xs ${STATUS_STYLES[r.status]}`}>
                    {r.status}
                  </span>
                  <span className="font-mono">{r.run_id}</span>
                  <span className="text-xs text-ink-faint">
                    {r.suites.join('+')} → {r.targets.join(', ')}
                  </span>
                  <span className="ml-auto text-xs text-ink-faint">
                    {duration(r)} · {relTime(r.started_at)}
                  </span>
                </button>
              ))}
            </div>
          </div>

          {active && (
            <div className="rounded-lg border border-line bg-panel/40 p-4">
              <div className="mb-3 flex items-center gap-3">
                <h2 className="text-sm font-semibold text-ink-dim">{active.run_id}</h2>
                <span className={`rounded border px-1.5 py-0.5 text-xs ${STATUS_STYLES[active.status]}`}>
                  {active.status}
                </span>
                {active.status === 'running' && (
                  <button
                    onClick={async () => {
                      await benchmarksApi.cancel(active.run_id)
                      await loadRuns()
                    }}
                    className="ml-auto rounded border border-gone/40 px-2 py-1 text-xs text-gone"
                  >
                    Cancel
                  </button>
                )}
              </div>

              {!!active.metrics?.length && (
                <div className="mb-4 overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="text-left text-xs uppercase text-ink-faint">
                      <tr>
                        <th className="py-1 pr-4">target</th>
                        <th className="py-1 pr-4">benchmark</th>
                        <th className="py-1 pr-4">metric</th>
                        <th className="py-1 text-right">value</th>
                      </tr>
                    </thead>
                    <tbody className="font-mono">
                      {active.metrics.map((m, i) => (
                        <tr key={i} className="border-t border-line/60">
                          <td className="py-1 pr-4">{m.target}</td>
                          <td className="py-1 pr-4 text-ink-dim">{m.benchmark}</td>
                          <td className="py-1 pr-4 text-ink-dim">{m.metric}</td>
                          <td className="py-1 text-right">
                            {typeof m.value === 'number' ? m.value.toFixed(3) : String(m.value)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {active.log_tail && (
                <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded bg-ground p-3 text-xs text-ink-dim">
                  {active.log_tail}
                </pre>
              )}
            </div>
          )}
        </section>
      </div>
    </AppShell>
  )
}
