'use client'

/**
 * ARIA - Operate: shared vocabulary and derivations
 *
 * Current model choices are grouped by host. Memory meters still derive
 * independent pools from observed rows; archived residents remain visible so
 * the UI never hides memory use merely because a deployment was retired.
 */
import { useState } from 'react'
import type { ModelServerFull, ServiceFull, UtilServer } from '@/lib/api/types'
import { normalizeState, type ServerState } from '@/components/ui/primitives'
import type { Toast } from '@/components/ui/controls'

export const POOL_LABELS: Record<string, string> = {
  'halo-gtt': 'Strix Halo',
  'r9700-vram': 'R9700',
  'corsair-nvidia-vram': 'RTX 3090',
  'host-ram': 'CPU',
  remote: 'off-box',
}

/** Memory meter ordering, independent of the host-grouped model list. */
export const POOL_ORDER = ['halo-gtt', 'r9700-vram', 'corsair-nvidia-vram', 'host-ram', 'remote'] as const

export const SOURCE_LABELS: Record<string, string> = {
  aria_override: 'set here',
  unit_dropin: 'unit drop-in',
  script_default: 'script default',
  declared_default: 'default',
  unset: 'unset',
}

/**
 * The state WORD, colour included — the old fleet list showed only a 7px dot,
 * which reads as nothing on a phone in daylight. Colours are tokens only.
 */
export const STATE_WORD: Record<ServerState, { word: string; tone: string }> = {
  running: { word: 'running', tone: 'text-live' },
  // The registry's "ready" means "unit not materialised yet — ARIA generates
  // it on first start". It is a STOPPED state; the shared normalizeState
  // paints it live-green, which misread as resident (verified against
  // model_servers.py _inspect, 2026-08-17).
  ready: { word: 'ready to start', tone: 'text-idle' },
  loading: { word: 'loading', tone: 'text-accent' },
  failed: { word: 'failed', tone: 'text-gone' },
  asleep: { word: 'asleep', tone: 'text-idle' },
  exited: { word: 'stopped', tone: 'text-idle' },
  absent: { word: 'weights absent', tone: 'text-gone' },
  external: { word: 'off-box', tone: 'text-idle' },
  unknown: { word: 'unknown', tone: 'text-ink-faint' },
}

/** CPU-only servers cost no GPU memory, which is why they coexist with a big model. */
export const isGpu = (s: ModelServerFull) =>
  !/cpu/i.test(s.backend_device || '') && (s.resident_gib_estimate ?? 0) > 0

export const serverState = (s: ModelServerFull): ServerState =>
  s.onbox === false && !s.remotely_operable ? 'external' : normalizeState(s.state)

/** Resident = actually holding (or filling) its memory pool. "ready" is NOT
 * resident — it is the registry's word for "startable, unit not created". */
export const isResident = (s: ModelServerFull) => {
  const st = serverState(s)
  return st === 'running' || st === 'loading'
}

export const MODEL_NAMES: Record<string, string> = {
  'Red-Qwen3.8-27B-PARO-INT5': 'Qwen3.8-27B PARO int5',
  'Qwen3.8-Flash-Next-CUDA-Halo-Candidate': 'Qwen Flash Next',
  'Red-Qwen3.8-27B-MXFP4': 'Qwen3.8-27B',
  'Red-Qwen3.8-Flash-Next-MXFP4': 'Qwen Flash Next',
  'Red-Qwen3.8-27B-PARO-MXFP4': 'Qwen3.8-27B PARO MXFP4',
  'Ridge-Qwen3.8-27B': 'Qwen3.8-27B (unverified)',
}
export const modelName = (slug: string) => MODEL_NAMES[slug] ?? slug
export const isModelChoice = (s: ModelServerFull) => s.catalog_visible ?? (s.slug in MODEL_NAMES)
export const modelHost = (s: ModelServerFull) =>
  s.host_machine?.replace(/^machine:/, '') ??
  (s.slug.startsWith('Red-') ? 'red' : s.slug.startsWith('Ridge-') ? 'ridge' :
    s.slug.startsWith('gemma-') ? 'mac' : 'corsair')

/** State for the DOT: "ready" must not glow live-green (see STATE_WORD.ready). */
export const dotState = (s: ModelServerFull) => {
  const st = serverState(s)
  return st === 'ready' ? 'exited' : st
}

export type Pool = { pool: string; label: string; used_gib: number; total_gib: number; spilling: boolean }

/**
 * The GPU pools, deduplicated from the rows. host-ram and remote are excluded
 * from the METERS deliberately: host RAM pressure is not what decides whether
 * a model fits, and off-box pools are not this machine's budget.
 */
export function derivePools(servers: ModelServerFull[]): Pool[] {
  const out = new Map<string, Pool>()
  for (const s of servers) {
    const p = s.memory_pool
    if (!p || p === 'host-ram' || p === 'remote' || s.pool_total_gib == null || out.has(p)) continue
    out.set(p, {
      pool: p,
      label: POOL_LABELS[p] ?? p,
      used_gib: s.pool_used_gib ?? 0,
      total_gib: s.pool_total_gib,
      spilling: !!s.pool_spilling,
    })
  }
  return [...out.values()].sort(
    (a, b) => POOL_ORDER.indexOf(a.pool as (typeof POOL_ORDER)[number]) - POOL_ORDER.indexOf(b.pool as (typeof POOL_ORDER)[number])
  )
}

export type FleetGroup = {
  pool: string
  label: string
  active: ModelServerFull[]
  retired: ModelServerFull[]
}

/**
 * Group current choices by host, with running models first. Retained records
 * are hidden unless resident; a model that unexpectedly runs stays observable.
 */
export function groupFleet(servers: ModelServerFull[], filter: string): FleetGroup[] {
  const q = filter.trim().toLowerCase()
  const match = (s: ModelServerFull) =>
    !q ||
    s.slug.toLowerCase().includes(q) ||
    modelName(s.slug).toLowerCase().includes(q) ||
    modelHost(s).includes(q) ||
    (s.description ?? '').toLowerCase().includes(q) ||
    (s.memory_pool ?? '').toLowerCase().includes(q)

  const groups = new Map<string, FleetGroup>()
  for (const s of servers) {
    if ((!isModelChoice(s) && !isResident(s)) || !match(s)) continue
    const pool = modelHost(s)
    let g = groups.get(pool)
    if (!g) {
      g = { pool, label: pool.charAt(0).toUpperCase() + pool.slice(1), active: [], retired: [] }
      groups.set(pool, g)
    }
    // A retired entry that is somehow running still belongs in the live list —
    // hiding a resident model behind "retired" would misreport the machine.
    if (s.startable === false && !isResident(s)) g.retired.push(s)
    else g.active.push(s)
  }

  const rank = (s: ModelServerFull) => (isResident(s) ? 0 : 1)
  for (const g of groups.values()) {
    g.active.sort((a, b) => rank(a) - rank(b) || (b.resident_gib_estimate ?? 0) - (a.resident_gib_estimate ?? 0))
    g.retired.sort((a, b) => (b.resident_gib_estimate ?? 0) - (a.resident_gib_estimate ?? 0))
  }
  const hosts = ['corsair', 'red', 'ridge', 'mac']
  return [...groups.values()].sort((a, b) => hosts.indexOf(a.pool) - hosts.indexOf(b.pool))
}

/** Services sorted unhealthy-first: this list exists to answer "is anything wrong?". */
export function sortServices(services: ServiceFull[]): ServiceFull[] {
  return [...services].sort((a, b) => {
    if (!!a.healthy !== !!b.healthy) return a.healthy ? 1 : -1
    if (a.expected_state !== b.expected_state) return a.expected_state === 'always_up' ? -1 : 1
    return a.slug.localeCompare(b.slug)
  })
}

export const utilFor = (util: UtilServer[] | undefined, slug: string) =>
  util?.find((u) => u.slug === slug && u.reachable)

/** Toast plumbing shared by the three operate surfaces (same as Inbox's). */
export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([])
  const push = (tone: Toast['tone'], text: string) => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, tone, text }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000)
  }
  return { toasts, push, dismiss: (id: number) => setToasts((t) => t.filter((x) => x.id !== id)) }
}
