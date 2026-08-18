'use client'

/**
 * ARIA - Operate: the fleet master list
 *
 * Selection is ROUTING (`/operate/servers/[slug]`), not useState — the audit's
 * finding was that tapping a fleet row on a phone "did nothing" because the
 * detail lived 1.2kpx below a 942px master list, and Back did nothing because
 * nothing was in the URL. Rows are Links, so a selection survives reload, Back
 * works, and a Signal deep-link lands on the right server.
 *
 * Grouped by MEMORY POOL because that is the actual constraint: entries in
 * different pools can be resident simultaneously, so a flat by-weight list
 * (the old ordering) implied a queue that does not exist. The state WORD is
 * printed on every row — the old 7px dot was the only state signal.
 */
import { useState } from 'react'
import Link from 'next/link'
import { useParams } from 'next/navigation'
import type { ModelServerFull, ModelServersFullResponse } from '@/lib/api/types'
import { Card, StatusDot } from '@/components/ui/primitives'
import { Disclosure, Field, Input } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Row, Stack } from '@/components/layout'
import { gib } from '@/lib/format'
import type { Resource } from '@/lib/swr'
import { STATE_WORD, dotState, groupFleet, isGpu, serverState } from './lib'

function FleetRow({ server, selected }: { server: ModelServerFull; selected: boolean }) {
  const st = serverState(server)
  const word = STATE_WORD[st]
  return (
    <li className="border-b border-line last:border-b-0">
      <Row
        as={Link}
        href={`/operate/servers/${encodeURIComponent(server.slug)}`}
        aria-current={selected || undefined}
        marker={<StatusDot state={dotState(server)} />}
        trailing={server.onbox === false ? 'off-box' : isGpu(server) ? gib(server.resident_gib_estimate) : 'cpu'}
        className={`border-l-2 px-2.5 py-1.5 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${
          selected ? 'border-accent bg-panel-2' : 'border-transparent hover:bg-panel-2'
        }`}
      >
        {/* Slugs discriminate at both ends and hold real information — let
            them wrap rather than hiding half behind an ellipsis+tooltip. */}
        <span className={`block wrap-anywhere font-mono text-label ${selected ? 'text-accent' : 'text-ink'}`}>
          {server.slug}
        </span>
        <span className={`text-micro ${word.tone}`}>{word.word}</span>
      </Row>
    </li>
  )
}

export function FleetList({ fleet }: { fleet: Resource<ModelServersFullResponse> }) {
  const [filter, setFilter] = useState('')
  const params = useParams<{ slug?: string }>()
  const selected = params?.slug ? decodeURIComponent(params.slug) : null

  return (
    <Card title="Fleet" hint={fleet.data ? `${fleet.data.servers.length} registered` : undefined} bodyClassName="p-0">
      <div className="border-b border-line px-2.5 py-2">
        <Field label="Filter">
          {/* coarse:text-title (1rem) works around the shared Input's `text-body`
              utility beating the base-layer 16px anti-focus-zoom rule — without
              it iOS zooms into the field and never zooms back out. */}
          <Input
            type="search"
            className="coarse:text-title"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="slug, pool, description…"
            aria-label="Filter the fleet"
          />
        </Field>
      </div>
      <Async r={fleet} skeletonRows={6}>
        {(d) => {
          const groups = groupFleet(d.servers, filter)
          if (groups.length === 0)
            return <p className="m-0 px-2.5 py-3 font-sans text-prose text-ink-faint">Nothing matches “{filter}”.</p>
          return (
            <Stack gap="none">
              {groups.map((g) => (
                <section key={g.pool} className="border-b border-line last:border-b-0">
                  <h3 className="m-0 bg-panel-2 px-2.5 py-1.5 text-micro font-medium uppercase tracking-[0.14em] text-ink-faint">
                    {g.label}
                    <span className="tnum ml-2 normal-case text-ink-faint">{g.active.length + g.retired.length}</span>
                  </h3>
                  <ul className="m-0 list-none p-0">
                    {g.active.map((s) => (
                      <FleetRow key={s.slug} server={s} selected={s.slug === selected} />
                    ))}
                  </ul>
                  {g.retired.length > 0 &&
                    // A filter means "show me everything that matches" — the
                    // retired fold stays open so matches are not hidden twice.
                    (filter.trim() ? (
                      <ul className="m-0 list-none p-0 opacity-70">
                        {g.retired.map((s) => (
                          <FleetRow key={s.slug} server={s} selected={s.slug === selected} />
                        ))}
                      </ul>
                    ) : (
                      <Disclosure
                        className="px-2.5 py-1"
                        summary={<span className="text-micro text-ink-faint">{g.retired.length} retired</span>}
                      >
                        <ul className="m-0 -mx-2.5 list-none p-0 opacity-70">
                          {g.retired.map((s) => (
                            <FleetRow key={s.slug} server={s} selected={s.slug === selected} />
                          ))}
                        </ul>
                      </Disclosure>
                    ))}
                </section>
              ))}
            </Stack>
          )
        }}
      </Async>
    </Card>
  )
}
