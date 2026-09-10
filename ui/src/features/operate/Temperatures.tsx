'use client'

/**
 * ARIA - Operate: temperature sensors
 *
 * `HostTemps` renders ONE host and is the unit the machine sections use, so a
 * box's temperature sits next to its memory and its resident model rather than
 * in a separate fleet-wide card the operator has to join up by hand.
 *
 * `Temperatures` remains for hosts no machine claims — telemetry from an
 * unrecognised node must stay visible rather than disappearing until someone
 * remembers to add it to the machine table.
 */
import { Card } from '@/components/ui/primitives'
import { Async } from '@/components/ui/Async'
import type { DevicesResponse, TemperatureHost, TemperatureSensor } from '@/lib/api/types'
import type { Resource } from '@/lib/swr'
import { useObservationClock } from '@/lib/swr'

function Sensor({ sensor }: { sensor: TemperatureSensor }) {
  const value = sensor.value_c
  const valid = value != null && Number.isFinite(value)
  const critical = valid && sensor.critical_c != null && value >= sensor.critical_c
  const high = valid && sensor.high_c != null && value >= sensor.high_c
  return (
    <li className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1 py-1 text-micro">
      <span className="min-w-0 flex-1 wrap-anywhere" title={`${sensor.device} · ${sensor.source}`}>
        {sensor.label}
        <span className="block text-ink-faint">{sensor.device}</span>
      </span>
      <span className={`tnum shrink-0 ${critical || high ? 'text-gone' : 'text-ink'}`}>
        {valid ? `${value.toFixed(1)} °C` : 'Unavailable'}
        {critical ? ' · critical limit' : high ? ' · high limit' : ''}
      </span>
    </li>
  )
}

/**
 * One host's sensors. `heading` is off by default inside a machine card, where
 * the card header already names the box; the standalone card turns it on.
 */
export function HostTemps({ host, heading = false }: { host: TemperatureHost; heading?: boolean }) {
  const now = useObservationClock()
  const age = (now - Date.parse(host.observed_at ?? '')) / 1000
  const fresh = Number.isFinite(age) && age >= -5 && age <= host.max_age_seconds
  const available = fresh && host.status === 'available'
  const main = host.sensors.filter((s) => s.kind === 'cpu' || s.kind === 'gpu')
  const other = host.sensors.filter((s) => s.kind !== 'cpu' && s.kind !== 'gpu')
  return (
    <section key={host.node} aria-label={`${host.node} temperatures`} className="min-w-0">
      {heading && <p className="m-0 text-label text-ink">{host.node}</p>}
      {available ? (
        <>
          <p className="m-0 text-micro text-ink-dim">Updated {Math.max(0, Math.floor(age))}s ago</p>
          <ul className="m-0 list-none p-0">
            {main.map((s) => (
              <Sensor key={s.id} sensor={s} />
            ))}
          </ul>
          {other.length > 0 && (
            <details>
              <summary className="flex min-h-11 cursor-pointer items-center text-micro text-ink-dim">
                Other sensors ({other.length})
              </summary>
              <ul className="m-0 list-none p-0">
                {other.map((s) => (
                  <Sensor key={s.id} sensor={s} />
                ))}
              </ul>
            </details>
          )}
        </>
      ) : (
        <p className="m-0 mt-1 text-micro text-ink-dim">
          {host.observed_at && !fresh ? 'Reading expired.' : 'No current reading.'} The host or its sensors may be
          unavailable.
        </p>
      )}
    </section>
  )
}

/** Fleet-wide fallback for hosts outside the machine table. */
export function Temperatures({ devices, hosts }: { devices: Resource<DevicesResponse>; hosts?: TemperatureHost[] }) {
  return (
    <Card title="Other hosts" hint="°C · live sensors">
      <Async
        r={devices}
        skeletonRows={3}
        isEmpty={() => !(hosts ?? []).length}
        empty="No temperature telemetry."
      >
        {() => (
          <div className="grid min-w-0 gap-4 sm:grid-cols-2">
            {(hosts ?? []).map((host) => (
              <HostTemps key={host.node} host={host} heading />
            ))}
          </div>
        )}
      </Async>
      <p className="m-0 mt-3 text-micro text-ink-faint">
        Monitoring does not wake sleeping hosts. Limits appear only when reported by the sensor.
      </p>
    </Card>
  )
}
