'use client'

/**
 * ARIA - memory visualisation
 *
 * There are TWO physical pools on this box, not three, and the old panel drew
 * ONE bar. Both facts were wrong in different directions:
 *
 *   System RAM (124.4 GiB)   ← the Strix Halo iGPU has no memory of its own;
 *                              its GTT allocation IS system RAM. So `halo-gtt`
 *                              and `host-ram` are two measurements of the same
 *                              DIMMs, and host-ram's used figure already
 *                              contains the iGPU's. Drawing them as two bars
 *                              claims ~248 GiB on a 124 GiB machine.
 *   R9700 VRAM (31.9 GiB)    ← the discrete card's own memory, genuinely
 *                              independent — and completely absent from the old
 *                              visualisation, which is how a resident 29.8 GiB
 *                              model could be invisible.
 *
 * So: one stacked bar for system memory (iGPU · other · free) and one for the
 * card. That a model on one does not compete with a model on the other is the
 * governing fact of this topology — with the standing exception that a dGPU
 * model too big for its VRAM spills into GTT, i.e. back into system RAM, which
 * is what `spilling` reports.
 */
import { Card, Chip, Notice } from '@/components/ui/primitives'
import { Stack } from '@/components/layout'
import { Async } from '@/components/ui/Async'
import { gib } from '@/lib/format'
import type { DevicesResponse } from '@/lib/api/types'

/**
 * Only what the projection needs. Deliberately structural rather than the full
 * server type: this panel is about memory, and the two server shapes in the
 * codebase disagree about unrelated fields.
 */
type MemoryClaimant = {
  slug: string
  memory_pool?: string | null
  resident_gib_estimate?: number | null
  also_uses?: string[] | null
}
import type { Resource } from '@/lib/swr'

type Segment = { key: string; gib: number; color: string; label: string }

function StackedMeter({
  title,
  note,
  total,
  segments,
  free,
  warn,
}: {
  title: string
  note: string
  total: number
  segments: Segment[]
  free: number
  warn?: string
}) {
  const pct = (v: number) => (total > 0 ? Math.max(0, Math.min(100, (v / total) * 100)) : 0)
  return (
    <div className="min-w-0">
      <div className="mb-1 flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-label text-ink">{title}</span>
        <span className="tnum ml-auto shrink-0 text-micro text-ink-dim">
          {gib(total - free)} of {gib(total)}
        </span>
      </div>

      <div className="flex h-6 overflow-hidden rounded-sm bg-track" role="img"
           aria-label={`${title}: ${segments.map((s) => `${s.label} ${gib(s.gib)}`).join(', ')}, ${gib(free)} free of ${gib(total)}`}>
        {segments.map((s) => (
          <div key={s.key} className={s.color} style={{ width: `${pct(s.gib)}%` }} />
        ))}
      </div>

      {/* A stacked bar is unreadable without a key — and each segment is a
          different kind of claim on the memory, not just a colour. */}
      <ul className="m-0 mt-1.5 flex list-none flex-wrap gap-x-3 gap-y-1 p-0">
        {segments.map((s) => (
          <li key={s.key} className="flex items-center gap-1.5 text-micro text-ink-dim">
            <span aria-hidden="true" className={`inline-block h-2 w-2 shrink-0 rounded-sm ${s.color}`} />
            {s.label} <span className="tnum text-ink">{gib(s.gib)}</span>
          </li>
        ))}
        <li className="flex items-center gap-1.5 text-micro text-ink-dim">
          <span aria-hidden="true" className="inline-block h-2 w-2 shrink-0 rounded-sm bg-track" />
          free <span className="tnum text-ink">{gib(free)}</span>
        </li>
      </ul>

      <p className="m-0 mt-1 text-micro text-ink-faint">{note}</p>
      {warn && <p className="m-0 mt-1 text-micro text-gone">{warn}</p>}
    </div>
  )
}

export function MemoryPools({
  devices,
  selected,
  residents,
}: {
  devices: Resource<DevicesResponse>
  /** Server whose footprint is projected onto its pool, if one is selected. */
  selected?: MemoryClaimant | null
  /** Everything resident, for the "stop this first" hint. */
  residents?: MemoryClaimant[]
}) {
  return (
    <Card title="Memory" hint="two physical pools">
      <Async r={devices} skeletonRows={3} isEmpty={(d) => !d.system && !(d.pools?.length ?? 0)} empty="No memory telemetry.">
        {(d) => {
          const sys = d.system
          const vram = (d.pools ?? []).find((p) => p.pool === 'r9700-vram')
          const dgpu = (d.devices ?? []).find((x) => x.discrete)

          // Where the selected server's footprint lands. A dGPU server is
          // charged to VRAM *and* to system RAM for its host-side runtime —
          // radiance holds 8-10 GiB permanently, which is what broke DS4's
          // preflight on 2026-08-16 despite the two sharing no VRAM.
          const add = selected?.resident_gib_estimate ?? 0
          const pool = selected?.memory_pool
          const addsToSystem = pool === 'halo-gtt' || pool === 'host-ram'
          const addsToVram = pool === 'r9700-vram'

          const sysSegments: Segment[] = sys
            ? [
                { key: 'igpu', gib: sys.igpu_gib ?? 0, color: 'bg-live', label: 'iGPU (GTT)' },
                { key: 'other', gib: sys.other_gib ?? 0, color: 'bg-idle', label: 'other' },
                ...(addsToSystem && add > 0
                  ? [{ key: 'add', gib: add, color: 'bg-accent', label: `${selected?.slug ?? 'selected'}` }]
                  : []),
              ]
            : []

          const sysFree = Math.max(0, (sys?.available_gib ?? 0) - (addsToSystem ? add : 0))
          const sysOver = sys ? add > (sys.available_gib ?? 0) && addsToSystem : false

          const vramTotal = vram?.total_gib ?? dgpu?.vram_total_gib ?? 0
          const vramUsed = vram?.used_gib ?? dgpu?.vram_used_gib ?? 0
          const vramSegments: Segment[] = [
            { key: 'used', gib: vramUsed, color: 'bg-live', label: 'resident' },
            ...(addsToVram && add > 0
              ? [{ key: 'add', gib: add, color: 'bg-accent', label: selected?.slug ?? 'selected' }]
              : []),
          ]
          const vramFree = Math.max(0, vramTotal - vramUsed - (addsToVram ? add : 0))
          const vramOver = addsToVram && add > vramTotal - vramUsed

          // Only same-pool residents can free space; listing everything sends
          // you to stop a model that frees nothing.
          const blockers = (residents ?? []).filter(
            (s) =>
              s.slug !== selected?.slug &&
              (addsToSystem
                ? s.memory_pool === 'halo-gtt' || s.memory_pool === 'host-ram'
                : s.memory_pool === pool)
          )

          return (
            <Stack gap="gap">
              {sys && (
                <StackedMeter
                  title="System memory"
                  note="Shared: the Strix Halo iGPU draws its GTT allocation from these same DIMMs, so this one bar is both the iGPU pool and host RAM."
                  total={sys.total_gib ?? 0}
                  segments={sysSegments}
                  free={sysFree}
                />
              )}

              {vramTotal > 0 && (
                <StackedMeter
                  title="R9700 VRAM"
                  note="The discrete card's own memory. A model here does not compete with one on the iGPU."
                  total={vramTotal}
                  segments={vramSegments}
                  free={vramFree}
                  warn={
                    vram?.spilling
                      ? 'Spilling into GTT — the card has started consuming system RAM, so the two pools are no longer independent.'
                      : undefined
                  }
                />
              )}

              {selected && add > 0 && (
                <p className="m-0 text-micro text-ink-dim">
                  <span className="text-ink">{selected.slug}</span> needs {gib(add)} from{' '}
                  {addsToVram ? 'the card’s VRAM' : 'system memory'}
                  {selected.also_uses?.length ? ' (plus host RAM for its runtime)' : ''}.
                </p>
              )}

              {(sysOver || vramOver) && (
                <Notice tone="warn">
                  {blockers.length > 0 ? (
                    <>
                      Not enough free memory. Stop one of:{' '}
                      <span className="break-all font-mono">{blockers.map((b) => b.slug).join(', ')}</span>
                    </>
                  ) : (
                    <>Not enough free memory, and nothing in this pool can be stopped to make room.</>
                  )}
                </Notice>
              )}

              {(d.devices?.length ?? 0) > 0 && (
                <ul className="m-0 list-none border-t border-line p-0 pt-2">
                  {d.devices.map((dev) => (
                    <li key={dev.card} className="flex min-w-0 flex-wrap items-baseline gap-x-2 py-0.5 text-micro">
                      <span className="shrink-0 text-ink-dim">{dev.card}</span>
                      <span className="min-w-0 text-ink">{dev.label}</span>
                      <span className="tnum ml-auto shrink-0 text-ink-faint">
                        {dev.discrete
                          ? `VRAM ${gib(dev.vram_used_gib)} / ${gib(dev.vram_total_gib)}`
                          : `GTT ${gib(dev.gtt_used_gib)} / ${gib(dev.gtt_total_gib)}`}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Stack>
          )
        }}
      </Async>
    </Card>
  )
}
