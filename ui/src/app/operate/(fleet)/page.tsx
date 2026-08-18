'use client'

/**
 * ARIA - /operate index: the phone spine.
 *
 * Also carries the `?server=` shim: the old page kept selection in
 * `?server=<slug>` links (Signal alerts and bookmarks still hold them), and
 * selection is a route segment now — so the shim reads the param and
 * `replace`s to `/operate/servers/[slug]`. Suspense is required around
 * `useSearchParams` or the whole route bails out of static rendering.
 */
import { Suspense, useEffect } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { useResource } from '@/lib/swr'
import { K } from '@/lib/api/endpoints'
import type {
  DevicesResponse,
  LlmRouteFull,
  ModelServersFullResponse,
  ServicesResponse,
  UtilizationResponse,
} from '@/lib/api/types'
import { Spine } from '@/features/operate/Spine'

function LegacyServerParamRedirect() {
  const router = useRouter()
  const params = useSearchParams()
  const server = params.get('server')
  useEffect(() => {
    if (server) router.replace(`/operate/servers/${encodeURIComponent(server)}`)
  }, [server, router])
  return null
}

export default function OperateIndexPage() {
  const fleet = useResource<ModelServersFullResponse>(K.modelServers, { tier: 'slow' })
  const services = useResource<ServicesResponse>(K.services, { tier: 'slow' })
  const route = useResource<LlmRouteFull>(K.llmRoute, { tier: 'slow' })
  const utilization = useResource<UtilizationResponse>(K.utilization, { tier: 'fast' })
  // Physical memory, read from the cards and /proc/meminfo rather than derived
  // from registry rows — the pools overlap (the iGPU's GTT IS system RAM) and
  // only this endpoint knows how.
  const devices = useResource<DevicesResponse>(K.devices, { tier: 'fast' })

  return (
    <>
      <Suspense fallback={null}>
        <LegacyServerParamRedirect />
      </Suspense>
      <Spine fleet={fleet} services={services} route={route} utilization={utilization} devices={devices} />
    </>
  )
}
