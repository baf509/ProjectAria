'use client'

/**
 * ARIA - Know: agents (read-only bindings + explicit rebind)
 *
 * Two measured defects shaped this page:
 *
 * 1. The old write path was a controlled <select> whose value was the agent's
 *    bound slug. When that slug was NOT in the on-box startable list (Ridge,
 *    retired entries), React silently rendered the FIRST option selected — the
 *    page claimed "bound to DS4" for agents bound to nothing of the sort.
 *    Binding is now read-only text + state chip; changing it is an explicit
 *    "Rebind…" Sheet whose select starts on a placeholder option.
 *
 * 2. Enable/Disable/Save called PUT /agents — admin-gated (X-Admin-Key, which
 *    this browser deliberately never holds) — but only AFTER starting the
 *    bound model server. The visible result: a 100 GiB model started loading,
 *    then the click 403'd. Those controls are gone; a Notice says why and
 *    where they live. (bind/unbind is a descriptive pairing, not admin-gated,
 *    so Rebind stays.)
 */
import { useMemo, useState } from 'react'
import { useResource, useAction } from '@/lib/swr'
import { K, bindModelServer, unbindModelServer } from '@/lib/api/endpoints'
import type { Agent, ModelServersResponse, ModelServer } from '@/lib/api/types'
import { Card, Chip, Code, Notice, Text, StateChip, normalizeState } from '@/components/ui/primitives'
import { Button, Field, Select, Sheet, Toasts } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Stack, Cluster, Grid } from '@/components/layout'
import { useKnowStats } from '@/features/know/knowStatus'
import { useToasts } from '@/features/know/useToasts'

const UNBIND = '__unbind__'

function AgentCard({
  agent,
  servers,
  fleetReady,
  onRebind,
}: {
  agent: Agent
  servers: ModelServer[]
  /** False while the registry call is in flight or failed. */
  fleetReady: boolean
  onRebind: (agent: Agent) => void
}) {
  const enabled = agent.enabled !== false
  const boundSlug = agent.model_server ?? null
  const server = boundSlug ? servers.find((s) => s.slug === boundSlug) : undefined
  // mode_metadata.icon holds a Lucide icon NAME ('code') for the widget, not a
  // glyph — rendered as text it produced "code Pi Coding Agent". Glyphs only.
  const icon = agent.mode_metadata?.icon && !/[a-z]/i.test(agent.mode_metadata.icon) ? agent.mode_metadata.icon : null

  return (
    <Card
      title={
        <span className="normal-case tracking-normal">
          {icon ? `${icon} ` : ''}
          {agent.name}
        </span>
      }
      actions={<Chip tone={enabled ? 'ok' : 'neutral'}>{enabled ? 'enabled' : 'disabled'}</Chip>}
    >
      <Stack gap="sm">
        {agent.description && <Text clamp={2}>{agent.description}</Text>}
        <div>
          <p className="m-0 text-micro uppercase tracking-[0.08em] text-ink-faint">Model</p>
          <Code>
            {agent.llm?.backend || '?'}/{agent.llm?.model || '?'}
          </Code>
        </div>
        <div>
          <p className="m-0 text-micro uppercase tracking-[0.08em] text-ink-faint">Model server</p>
          {boundSlug ? (
            <Cluster gap="gap-2" className="mt-0.5">
              <Code>{boundSlug}</Code>
              {server ? (
                <StateChip
                  state={normalizeState(server.state, server.weights_present !== false)}
                  note={server.onbox === false ? 'pinned' : undefined}
                />
              ) : fleetReady ? (
                // A binding to a slug the registry no longer knows: say so
                // instead of pretending it is the first thing in a dropdown.
                <Chip tone="warn">not in registry</Chip>
              ) : (
                // The registry call (~9s worst case) has not answered yet —
                // "loading" must never render as "not in registry".
                <Chip>registry…</Chip>
              )}
            </Cluster>
          ) : (
            <p className="m-0 font-sans text-prose text-ink-faint">not bound</p>
          )}
        </div>
        <Cluster>
          {server && server.onbox === false ? (
            <span className="text-micro text-ink-faint">off-box — pinned to its own hardware</span>
          ) : (
            <Button onClick={() => onRebind(agent)}>Rebind…</Button>
          )}
        </Cluster>
      </Stack>
    </Card>
  )
}

export default function AgentsPage() {
  const toasts = useToasts()
  const run = useAction()
  const agents = useResource<Agent[]>(K.agents, { tier: 'lazy' })
  // The fleet payload is shared with /operate via the common SWR key; slow
  // tier because bindings change on the order of days, not seconds.
  const fleet = useResource<ModelServersResponse>(K.modelServers, { tier: 'slow' })

  const [rebinding, setRebinding] = useState<Agent | null>(null)
  const [choice, setChoice] = useState('')
  const [busy, setBusy] = useState(false)

  const servers = useMemo(() => fleet.data?.servers ?? [], [fleet.data])
  const options = useMemo(() => servers.filter((s) => s.onbox !== false && s.startable !== false), [servers])

  const enabledCount = (agents.data ?? []).filter((a) => a.enabled !== false).length

  useKnowStats([
    { label: 'AGENTS', value: agents.data?.length ?? '—' },
    { label: 'ENABLED', value: enabledCount, tone: 'ok' },
  ])

  function openRebind(agent: Agent) {
    setChoice('')
    setRebinding(agent)
  }

  async function applyRebind() {
    if (!rebinding || !choice) return
    const slug = rebinding.slug || rebinding.id
    setBusy(true)
    const ok = await run(
      () => (choice === UNBIND ? unbindModelServer(slug) : bindModelServer(choice, slug)),
      { invalidate: ['/agents', '/infrastructure/model-servers'], onError: (e) => toasts.warn(e.message) }
    )
    setBusy(false)
    if (ok !== undefined) {
      toasts.ok(choice === UNBIND ? `${rebinding.name} unbound` : `${rebinding.name} → ${choice}`)
      setRebinding(null)
    }
  }

  return (
    <>
      <Stack>
        <Notice tone="info">
          Bindings are descriptive: they record which server an agent runs on, and one agent per server is enforced.
          Enable/disable and agent editing are <b>admin-gated</b> (PUT /agents requires <Code>X-Admin-Key</Code>, which
          this browser never holds — the old buttons started the bound model server and <i>then</i> 403&apos;d). Use the
          CLI or MCP with the admin key for those.
        </Notice>

        <Async r={agents} skeletonRows={5} isEmpty={(d) => d.length === 0} empty="No agents configured.">
          {(items) => (
            <Grid min="20rem">
              {items.map((agent) => (
                <AgentCard key={agent.id} agent={agent} servers={servers} fleetReady={fleet.data !== undefined} onRebind={openRebind} />
              ))}
            </Grid>
          )}
        </Async>
        {fleet.error && fleet.data === undefined && (
          <Notice tone="warn">Model-server registry unavailable — binding states cannot be shown.</Notice>
        )}
      </Stack>

      <Sheet open={rebinding !== null} onClose={() => setRebinding(null)} title={`Rebind ${rebinding?.name ?? ''}`}>
        <Stack gap="sm">
          <p className="m-0 font-sans text-prose text-ink-dim">
            Currently: <Code>{rebinding?.model_server || 'not bound'}</Code>
          </p>
          <Field label="Bind to" hint="on-box startable servers only; binding does not start anything">
            <Select value={choice} onChange={(e) => setChoice(e.target.value)} className="coarse:text-title">
              {/* The placeholder is the fix for the false-"bound to DS4" bug:
                  a select must never default to a real server. */}
              <option value="" disabled>
                — choose a server —
              </option>
              {options.map((s) => (
                <option key={s.slug} value={s.slug}>
                  {s.slug} ({s.state || 'unknown'})
                </option>
              ))}
              {rebinding?.model_server && <option value={UNBIND}>(unbind)</option>}
            </Select>
          </Field>
          <Cluster>
            <Button variant="primary" busy={busy} disabled={!choice} onClick={() => void applyRebind()}>
              Apply
            </Button>
            <Button onClick={() => setRebinding(null)}>Cancel</Button>
          </Cluster>
        </Stack>
      </Sheet>
      <Toasts toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </>
  )
}
