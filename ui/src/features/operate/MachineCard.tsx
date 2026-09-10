'use client'

/**
 * ARIA - Operate: one machine, everything about it
 *
 * Residency, memory and temperature for a single box, in that order: what is
 * loaded is the question being asked, the memory is why it fits (or why the
 * next thing will not), and the temperature is the reason to care whether it
 * has been loaded for a long time.
 *
 * The controls slot takes whatever that machine can actually be told to do —
 * they differ per box (Corsair has a standing loadout, Red swaps between two
 * models, the Mac has none), so the card does not try to generalise them.
 */
import Link from 'next/link'
import type { ReactNode } from 'react'
import { Card, Chip, EmptyState, StatusDot, Text } from '@/components/ui/primitives'
import { Row, Stack } from '@/components/layout'
import { gib, pct } from '@/lib/format'
import { STATE_WORD, dotState, modelName, serverState } from './lib'
import { StackedMeter } from './MemoryPools'
import { HostTemps } from './Temperatures'
import type { Machine } from './machines'

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <h3 className="m-0 mb-1.5 text-micro font-medium uppercase tracking-[0.14em] text-ink-faint">{label}</h3>
      {children}
    </div>
  )
}

export function MachineCard({ machine, children }: { machine: Machine; children?: ReactNode }) {
  const { spec, residents, meters, temps, remoteStatus } = machine

  return (
    <Card
      title={spec.title}
      hint={spec.hint}
      actions={
        remoteStatus && remoteStatus !== 'online' ? (
          <Chip tone="neutral">{remoteStatus}</Chip>
        ) : undefined
      }
    >
      <Stack gap="gap">
        <Section label="Resident now">
          {residents.length === 0 ? (
            <EmptyState>Nothing resident. {spec.role}</EmptyState>
          ) : (
            <ul className="m-0 list-none p-0">
              {residents.map(({ server, util, serving, pinned }) => {
                const st = serverState(server)
                const slots =
                  util && util.busy_slots != null && util.total_slots != null
                    ? `${util.busy_slots}/${util.total_slots} busy`
                    : null
                return (
                  <li key={server.slug} className="border-b border-line last:border-b-0">
                    <Row
                      as={Link}
                      href={`/operate/servers/${encodeURIComponent(server.slug)}`}
                      marker={<StatusDot state={dotState(server)} />}
                      trailing={server.resident_gib_estimate ? gib(server.resident_gib_estimate) : 'cpu'}
                      className="px-0 py-1.5 hover:bg-panel-2"
                    >
                      <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="wrap-anywhere font-mono text-label text-ink">{modelName(server.slug)}</span>
                        {serving && <Chip tone="accent">serving</Chip>}
                        {pinned && <Chip tone="neutral">pinned</Chip>}
                      </span>
                      <span className={`text-micro ${STATE_WORD[st].tone}`}>
                        {STATE_WORD[st].word}
                        {slots ? ` · ${slots}` : ''}
                        {util?.saturated ? ' · QUEUING' : ''}
                        {util?.slot_utilisation != null ? ` · ${pct(util.slot_utilisation)}` : ''}
                      </span>
                    </Row>
                  </li>
                )
              })}
            </ul>
          )}
        </Section>

        {children}

        {meters.length > 0 && (
          <Section label="Memory">
            <Stack gap="gap">
              {meters.map((m) => (
                <StackedMeter
                  key={m.key}
                  title={m.title}
                  note={m.note}
                  total={m.total_gib}
                  segments={m.segments}
                  free={m.free_gib}
                  warn={m.warn}
                />
              ))}
            </Stack>
          </Section>
        )}

        {/* An off-box machine that is asleep reports no pools at all. Say so,
            rather than leaving a machine card with a silent gap where its
            memory should be. */}
        {meters.length === 0 && spec.id !== 'mac' && (
          <Section label="Memory">
            <Text>
              No current hardware telemetry{remoteStatus ? ` — the host is ${remoteStatus}` : ''}. Monitoring does not
              wake a sleeping host.
            </Text>
          </Section>
        )}

        {temps && (
          <Section label="Temperatures">
            <HostTemps host={temps} />
          </Section>
        )}
      </Stack>
    </Card>
  )
}
