'use client'

/**
 * ARIA - Operate: the machine as the organising unit
 *
 * Operate used to be organised by *kind* — one Memory card, one Temperatures
 * card, one Resident card, each internally split by host. Answering "what is
 * Red doing?" meant reading three cards and joining them by hand, and the
 * Corsair loadout button sat in a fourth with no statement of what was already
 * resident on that box.
 *
 * A machine is the thing an operator actually reasons about: it has GPUs, a
 * memory budget those GPUs draw from, a temperature, and either a resident
 * model or not. So this module assembles one view-model per machine and the
 * page renders those in order.
 *
 * Identity is a small fixed table rather than something derived. The three
 * naming systems in play do NOT agree — the telemetry node is `corsair-ai`,
 * the registry has no `host_machine` at all (it is null on every row today, so
 * `modelHost` falls back to a slug prefix), and the docs say "Corsair". A
 * lookup table is the honest way to state a mapping that is not derivable.
 */
import type {
  DevicesResponse,
  GpuDevice,
  LlmRouteFull,
  MemoryPool,
  ModelServerFull,
  SystemMemory,
  TemperatureHost,
  UtilServer,
} from '@/lib/api/types'
import { isResident, modelHost, utilFor } from './lib'

export type MachineId = 'corsair' | 'red' | 'ridge' | 'mac'

export type MachineSpec = {
  id: MachineId
  title: string
  /** The hardware, in the header. */
  hint: string
  /** How this machine identifies itself in temperature/remote telemetry. */
  node: string
  /** What the machine is for — one line, shown when nothing is resident. */
  role: string
}

/**
 * Display order is deliberate and matches how the fleet is actually operated:
 * the standing model plane first, the on-demand boxes next, the control plane
 * last (it serves no models — it runs ARIA).
 */
export const MACHINES: readonly MachineSpec[] = [
  {
    id: 'corsair',
    title: 'Corsair',
    hint: 'Strix Halo + RTX 3090',
    node: 'corsair-ai',
    role: 'The standing model plane. Qwen Flash Next is expected to be resident here.',
  },
  {
    id: 'red',
    title: 'Red',
    hint: '2 × Radeon AI PRO R9700',
    node: 'red-linux',
    role: 'One model at a time. Wakes on demand; sleeping is normal.',
  },
  {
    id: 'ridge',
    title: 'Ridge',
    hint: 'remote CUDA',
    node: 'ridge',
    role: 'On demand. Readiness on the current card is unverified.',
  },
  {
    id: 'mac',
    title: 'MacBook Pro',
    hint: 'ARIA control plane',
    node: 'bens-macbook-pro',
    role: 'Runs ARIA, Mongo, Hermes and the model forwards. Serves no models.',
  },
] as const

const BY_NODE = new Map(MACHINES.map((m) => [m.node, m]))

/** Which machine a registry row belongs to. `modelHost` already encodes this. */
export function machineOf(server: ModelServerFull): MachineId {
  const host = modelHost(server)
  return (MACHINES.find((m) => m.id === host)?.id ?? 'corsair') as MachineId
}

/** A memory bar to draw for a machine. */
export type MachineMeter = {
  key: string
  title: string
  total_gib: number
  /** Stacked segments, in draw order. */
  segments: { key: string; gib: number; color: string; label: string }[]
  free_gib: number
  note: string
  warn?: string
}

export type ResidentModel = {
  server: ModelServerFull
  util?: UtilServer
  /** This model answers requests that name no model. */
  serving: boolean
  /** The operator pinned this model as the default. */
  pinned: boolean
}

export type Machine = {
  spec: MachineSpec
  /** Every registry row that belongs here, current choices and retired alike. */
  servers: ModelServerFull[]
  residents: ResidentModel[]
  meters: MachineMeter[]
  temps?: TemperatureHost
  /** Present for off-box machines: the node ARIA polls for hardware. */
  remoteStatus?: string
}

function systemMeter(system: SystemMemory): MachineMeter {
  return {
    key: 'system',
    title: 'System memory',
    total_gib: system.total_gib ?? 0,
    segments: [
      { key: 'igpu', gib: system.igpu_gib ?? 0, color: 'bg-live', label: 'iGPU (GTT)' },
      { key: 'other', gib: system.other_gib ?? 0, color: 'bg-idle', label: 'other' },
    ],
    free_gib: Math.max(0, system.available_gib ?? 0),
    note: 'Shared: the Strix Halo iGPU draws its GTT allocation from these DIMMs, so this one bar is both the iGPU pool and host RAM.',
  }
}

function vramMeter(pool: MemoryPool, device?: GpuDevice): MachineMeter {
  const total = pool.total_gib ?? device?.vram_total_gib ?? 0
  const used = pool.used_gib ?? device?.vram_used_gib ?? 0
  const detail = [
    device?.utilization_pct != null ? `GPU ${device.utilization_pct}%` : null,
    device?.temperature_c != null ? `${device.temperature_c} °C` : null,
    device?.power_watts != null ? `${device.power_watts} W` : null,
  ].filter(Boolean)
  return {
    key: pool.pool,
    title: pool.label,
    total_gib: total,
    segments: [{ key: 'used', gib: used, color: 'bg-live', label: 'resident' }],
    free_gib: pool.free_gib ?? Math.max(0, total - used),
    note: `The card's own memory${detail.length ? ` · ${detail.join(' · ')}` : ''}. Allocation includes the reserved KV cache.`,
    warn: pool.spilling
      ? 'Spilling into host memory — this pool is no longer independent of system RAM.'
      : undefined,
  }
}

/**
 * Assemble every machine's view-model.
 *
 * The `devices` payload is shaped around the box ARIA's model plane runs on:
 * `system`/`pools`/`devices` are Corsair's, `remote_hosts` carries the others.
 * Temperature hosts are keyed separately and cover the Mac too.
 */
export function buildMachines(
  servers: ModelServerFull[],
  devices: DevicesResponse | undefined,
  util: UtilServer[] | undefined,
  route: LlmRouteFull | undefined
): Machine[] {
  const temps = new Map((devices?.temperature_hosts ?? []).map((h) => [h.node, h]))
  const remotes = new Map((devices?.remote_hosts ?? []).map((h) => [h.node, h]))

  return MACHINES.map((spec) => {
    const own = servers.filter((s) => machineOf(s) === spec.id)
    const residents: ResidentModel[] = own.filter(isResident).map((server) => ({
      server,
      util: utilFor(util, server.slug),
      serving: route?.serving === server.slug,
      pinned: route?.pinned === server.slug,
    }))

    const meters: MachineMeter[] = []
    if (spec.id === 'corsair') {
      if (devices?.system) meters.push(systemMeter(devices.system))
      for (const pool of devices?.pools ?? []) {
        // host-ram and halo-gtt are already the system bar; drawing them again
        // claims ~248 GiB on a 124 GiB machine.
        if (pool.backing !== 'device') continue
        meters.push(vramMeter(pool, (devices?.devices ?? []).find((d) => d.pool === pool.pool)))
      }
    } else {
      const remote = remotes.get(spec.node)
      for (const pool of remote?.hardware?.pools ?? []) {
        meters.push(vramMeter(pool, remote?.hardware?.devices.find((d) => d.pool === pool.pool)))
      }
    }

    return {
      spec,
      servers: own,
      residents,
      meters,
      temps: temps.get(spec.node),
      remoteStatus: spec.id === 'corsair' ? undefined : remotes.get(spec.node)?.status,
    }
  })
}

/**
 * Temperature hosts that no machine claims. Telemetry for an unknown node is
 * still telemetry — hiding it would make a new box invisible until someone
 * remembered to edit the table above.
 */
export function unclaimedTemperatureHosts(devices: DevicesResponse | undefined): TemperatureHost[] {
  return (devices?.temperature_hosts ?? []).filter((h) => !BY_NODE.has(h.node))
}
