'use client'

import Link from 'next/link'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Card, Notice, Text } from '@/components/ui/primitives'
import { Button } from '@/components/ui/controls'
import { Cluster, Stack } from '@/components/layout'
import { api, hasAdminKey } from '@/lib/http'
import { K, modelServerAction, setLlmRoute } from '@/lib/api/endpoints'
import type { InferenceTrace, LlmRouteFull, ModelServerFull, ModelServersFullResponse, UtilizationResponse } from '@/lib/api/types'
import type { Resource } from '@/lib/swr'
import { isResident, modelName } from './lib'

const RED_MODELS = ['Red-Qwen3.8-27B-MXFP4', 'Red-Qwen3.8-Flash-Next-MXFP4'] as const
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

async function waitFor(slug: string, running: boolean) {
  const deadline = Date.now() + 20 * 60_000
  while (Date.now() < deadline) {
    const server = await api<ModelServerFull>(K.modelServer(slug))
    if (running && server.state === 'running') return
    if (!running && ['stopped', 'exited', 'ready', 'not_created', 'dead', 'asleep'].includes(server.state ?? '')) return
    if (running && ['dead', 'failed'].includes(server.state ?? '')) throw new Error(`${modelName(slug)} failed to load.`)
    await delay(5_000)
  }
  throw new Error(`${modelName(slug)} did not become ${running ? 'ready' : 'stopped'} within 20 minutes. Check its details before retrying.`)
}

export function RedModels({ fleet, route, utilization }: {
  fleet: Resource<ModelServersFullResponse>
  route: Resource<LlmRouteFull>
  utilization: Resource<UtilizationResponse>
}) {
  const [progress, setProgress] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const [callers, setCallers] = useState<string>('')
  const inFlight = useRef(false)
  const models = RED_MODELS.map(slug => fleet.data?.servers.find(s => s.slug === slug))
  const loaded = models.find(s => s && isResident(s))
  const loadedUtil = utilization.data?.servers?.find(s => s.slug === loaded?.slug)
  const activityUnknown = Boolean(loaded && (!loadedUtil?.reachable || loadedUtil.busy_slots == null))
  const activeRequests = (utilization.data?.servers ?? []).filter(s => RED_MODELS.includes(s.slug as typeof RED_MODELS[number]))
    .reduce((n, s) => n + (s.busy_slots ?? 0), 0)
  const loading = models.some(s => s?.state === 'loading' || s?.state === 'starting')
  const busy = Boolean(progress) || loading || activeRequests > 0 || activityUnknown

  // Which clients have used Red recently. The operator may not know where a
  // Pi session is running, and "Red is busy" without a name is not actionable.
  const recentCallers = useCallback(async (): Promise<string> => {
    try {
      const traces = await api<InferenceTrace[]>(K.usageTraces(1, 200))
      const callers = [...new Set(traces
        .filter(t => t.model && RED_MODELS.includes(t.model as typeof RED_MODELS[number]))
        .map(t => t.caller).filter((c): c is string => Boolean(c)))]
      return callers.slice(0, 4).join(', ')
    } catch {
      return ''   // attribution is a convenience; never block the swap on it
    }
  }, [])

  function confirmInterrupt(): boolean {
    return window.confirm(
      `Interrupt ${activeRequests} in-flight Red request${activeRequests === 1 ? '' : 's'}` +
      `${callers ? ` from ${callers}` : ''}?\n\n` +
      'Those requests fail immediately and the work in them is lost. ' +
      'Their sessions stay open and can be retried once the new model is loaded.')
  }

  useEffect(() => {
    if (activeRequests === 0) { setCallers(''); return }
    let cancelled = false
    void recentCallers().then(who => { if (!cancelled) setCallers(who) })
    return () => { cancelled = true }
  }, [activeRequests, recentCallers])

  async function change(next: string | null, override = false) {
    if (inFlight.current) return
    inFlight.current = true
    setError(null); setDone(null); setProgress('Checking Red…')
    try {
      // Re-read before acting: background polls and another tab can change residency.
      const [fresh, util, currentRoute] = await Promise.all([
        api<ModelServersFullResponse>('/infrastructure/model-servers'),
        api<UtilizationResponse>(K.utilization),
        api<LlmRouteFull>(K.llmRoute),
      ])
      const red = fresh.servers.filter(s => RED_MODELS.includes(s.slug as typeof RED_MODELS[number]))
      const selected = red.find(s => s.slug === next)
      if (next && (!selected || selected.startable !== true || selected.catalog_visible === false))
        throw new Error('This model is not available in the active Aria release yet.')
      if (red.some(s => s.state === 'loading' || s.state === 'starting'))
        throw new Error('Red is already loading a model. Wait for it to finish.')
      if (!override && util.servers?.some(s => RED_MODELS.includes(s.slug as typeof RED_MODELS[number]) && (s.busy_slots ?? 0) > 0)) {
        const who = await recentCallers()
        throw new Error(
          `Red is serving requests${who ? ` for ${who}` : ''}. Finish or stop those sessions, ` +
          'or use "Switch anyway" to interrupt them.')
      }
      const previous = red.filter(isResident)
      // Route changes are admin-gated. Refuse before unloading, rather than
      // discovering the missing credential after a successful replacement.
      if (currentRoute.pinned && previous.some(s => s.slug === currentRoute.pinned) &&
          currentRoute.pinned !== next && !hasAdminKey())
        throw new Error('Red is the pinned default. Add your admin key under More or in the sidebar before changing its model.')
      if (previous.some(s => {
        const activity = util.servers?.find(u => u.slug === s.slug)
        return !activity?.reachable || activity.busy_slots == null
      })) throw new Error('Red activity is unavailable. Retry when its current requests can be checked.')
      for (const s of previous.filter(s => s.slug !== next)) {
        if (s.bound_agents?.length && !override) throw new Error(`${modelName(s.slug)} is assigned to ${s.bound_agents.join(', ')}. Release those assignments, or use "Switch anyway".`)
        setProgress(`Unloading ${modelName(s.slug)}…`)
        await modelServerAction(s.slug, 'stop')
        await waitFor(s.slug, false)
      }
      if (next && !previous.some(s => s.slug === next)) {
        setProgress(`Loading ${modelName(next)}… This can take a few minutes.`)
        await modelServerAction(next, 'start')
        await waitFor(next, true)
      }
      // Preserve an explicit Red default across a successful swap. Loading Red
      // otherwise does not replace Corsair or change the user's routing choice.
      if (currentRoute.pinned && previous.some(s => s.slug === currentRoute.pinned) && currentRoute.pinned !== next)
        await setLlmRoute(next)
      setDone(next ? `${modelName(next)} is loaded on Red.` : 'Red is unloaded. Both models remain available to load.')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      await Promise.allSettled([fleet.refresh(), route.refresh(), utilization.refresh()])
      inFlight.current = false; setProgress(null)
    }
  }

  return <Card title="Red models" hint="Two R9700s · one model at a time">
    <Stack gap="sm">
      <Text>{loaded ? `Loaded: ${modelName(loaded.slug)}` : 'No model is loaded on Red.'}</Text>
      {RED_MODELS.map((slug, i) => {
        const server = models[i]
        const resident = Boolean(server && isResident(server))
        return <section key={slug} aria-label={modelName(slug)} className="rounded-sm border border-line p-3">
          <Cluster>
            <div className="min-w-0 flex-1">
              <b className="text-label">{modelName(slug)}</b>
              <Text>{i === 0 ? '27B · 256K context · up to 8 requests' : 'Flash Next · 256K context · 1 request'}</Text>
            </div>
            <Button variant={resident ? 'default' : 'primary'}
              disabled={busy || !fleet.data || (!resident && server?.startable !== true)}
              aria-label={resident ? `Unload ${modelName(slug)}` : `${loaded ? 'Switch to' : 'Load'} ${modelName(slug)}`}
              onClick={() => change(resident ? null : slug)}>
              {resident ? 'Unload' : loaded ? 'Switch' : 'Load'}
            </Button>
            {server && <Link className="inline-flex min-h-control min-w-control items-center justify-center text-micro text-accent underline" href={`/operate/servers/${encodeURIComponent(slug)}`}>Details</Link>}
          </Cluster>
          {!server && <Text>Available after the pending Aria update is activated.</Text>}
          {server?.startable === false && <Text>{server.not_startable_reason || 'Unavailable in this deployment.'}</Text>}
        </section>
      })}
      <Text>Switch unloads the current Red model, then loads the selected one. Loading can take a few minutes.</Text>
      {activeRequests > 0 && <Notice tone="info">
        Red is serving {activeRequests} request{activeRequests === 1 ? '' : 's'}{callers ? ` for ${callers}` : ''}. Switching is available when they finish,
        or interrupt them with Switch anyway on the model you want.
      </Notice>}
      {activeRequests > 0 && loaded && <Cluster>
        {RED_MODELS.filter(slug => slug !== loaded.slug).map(slug =>
          <Button key={slug} variant="default" disabled={Boolean(progress)}
            aria-label={`Switch anyway to ${modelName(slug)}`}
            onClick={() => { if (confirmInterrupt()) void change(slug, true) }}>
            Switch anyway to {modelName(slug)}
          </Button>)}
        <Button variant="default" disabled={Boolean(progress)} aria-label="Unload Red anyway"
          onClick={() => { if (confirmInterrupt()) void change(null, true) }}>Unload anyway</Button>
      </Cluster>}
      {activityUnknown && <Notice tone="info">Checking Red activity before enabling model changes…</Notice>}
      {progress && <div role="status"><Notice tone="info">{progress}</Notice></div>}
      {done && <div role="status"><Notice tone="info">{done}</Notice></div>}
      {error && <div role="alert"><Notice tone="warn">{error}</Notice></div>}
    </Stack>
  </Card>
}
