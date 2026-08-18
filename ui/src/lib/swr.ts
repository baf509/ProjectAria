'use client'

/**
 * ARIA - the data layer
 *
 * Replaces nine hand-rolled `useEffect + setInterval` pollers at five different
 * cadences, none of which paused when the tab was hidden, deduped, cancelled on
 * unmount, sequenced responses, or backed off on failure. Consequences measured
 * before this existed: the 73KB fleet payload fetched independently by three
 * routes; a 10s interval overlapping its own 8.8s request so an older payload
 * could overwrite a newer one; every navigation starting from `useState([])`
 * (i.e. a blank card); and 14 requests committed in ONE transition so every
 * dashboard tab was empty until the slowest returned.
 */
import { useRef, useState } from 'react'
import useSWR, { SWRConfiguration, mutate as globalMutate } from 'swr'
import { api, ApiError, fetcher } from './http'

/**
 * Freshness tiers. The API rate-limits 120 requests / 60s per key+path and
 * every client shares one key, so nothing may poll faster than 30/min per path
 * per device (phone + laptop + a reconnect burst otherwise trips it).
 */
export const TIER = {
  live: 5_000,
  fast: 10_000,
  normal: 15_000,
  slow: 30_000,
  lazy: 60_000,
  static: 0,
} as const

export type Tier = keyof typeof TIER

/** ±10% jitter so six keys do not fire in lockstep when the tab regains focus. */
function jitter(ms: number) {
  return ms === 0 ? 0 : Math.round(ms * (0.9 + Math.random() * 0.2))
}

export type ResourceOptions = {
  tier?: Tier
  enabled?: boolean
  /** Extra SWR options for the rare case a panel needs them. */
  swr?: SWRConfiguration
}

export type Resource<T> = {
  data: T | undefined
  error: ApiError | undefined
  isLoading: boolean
  /** Data is present but the last revalidation failed. */
  stale: boolean
  updatedAt: number | undefined
  refresh: () => Promise<T | undefined>
  mutate: ReturnType<typeof useSWR<T, ApiError>>['mutate']
}

export function useResource<T>(
  key: string | null,
  { tier = 'normal', enabled = true, swr }: ResourceOptions = {}
): Resource<T> {
  const active = enabled && key !== null
  const base = TIER[tier]

  // Consecutive failures, for the polling backoff. SWR's own errorRetry covers
  // the immediate retry; this is the separate question of how fast to keep
  // POLLING an endpoint that is currently failing.
  const failures = useRef(0)
  // When the data was last CONFIRMED (a successful fetch), which is what a
  // freshness stamp means. Deliberately not `Date.now()` at render time — that
  // reads "0s ago" forever, including while showing minutes-old cached data,
  // which is precisely the lie the stamp exists to prevent.
  const confirmedAt = useRef<number | undefined>(undefined)
  const [, forceRender] = useState(0)

  const res = useSWR<T, ApiError>(active ? key : null, fetcher, {
    refreshInterval: () => {
      // Belt (this) and braces (refreshWhenHidden) — a backgrounded PWA must
      // not burn requests nobody is looking at.
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return 0
      if (base === 0) return 0
      const n = failures.current
      if (n > 0) return Math.min(base * 2 ** n, 120_000)
      return jitter(base)
    },
    refreshWhenHidden: false,
    refreshWhenOffline: false,
    revalidateOnFocus: true,
    revalidateOnReconnect: true,
    keepPreviousData: true,
    // Must cover a navigation round trip, or every route change refetches
    // everything: at the old flat 2s, /operate -> /inbox -> /operate refetched
    // the 73KB fleet payload because the user took longer than two seconds to
    // come back. Held just under the poll interval so polling still fires.
    dedupingInterval: base === 0 ? 60_000 : Math.max(2_000, Math.round(base * 0.9)),
    errorRetryInterval: 5_000,
    errorRetryCount: 5,
    shouldRetryOnError: (err) => !(err instanceof ApiError) || err.retryable,
    onSuccess: () => {
      const hadFailures = failures.current > 0
      failures.current = 0
      confirmedAt.current = Date.now()
      // Recovering from a backoff has to re-render, or the interval function is
      // never consulted again at the faster rate.
      if (hadFailures) forceRender((n) => n + 1)
    },
    onError: () => {
      failures.current += 1
    },
    ...swr,
  })

  return {
    data: res.data,
    error: res.error,
    isLoading: res.isLoading && res.data === undefined,
    stale: res.error !== undefined && res.data !== undefined,
    updatedAt: confirmedAt.current,
    refresh: () => res.mutate(),
    mutate: res.mutate,
  }
}

/**
 * Actions. Optimistic where the server just records a fact (ack, decide,
 * accept); NEVER optimistic for state machines (start/stop a model server takes
 * minutes and can be refused by the RAM gate) — those show a pending state and
 * let the next poll tell the truth.
 */
export function useAction() {
  return async function run<T>(
    fn: () => Promise<T>,
    opts: { invalidate?: string[]; onError?: (e: ApiError) => void } = {}
  ): Promise<T | undefined> {
    try {
      const out = await fn()
      for (const key of opts.invalidate ?? []) {
        // Prefix invalidation: '/alerts' refreshes every alerts query variant.
        void globalMutate((k) => typeof k === 'string' && k.startsWith(key), undefined, { revalidate: true })
      }
      return out
    } catch (err) {
      const e = err instanceof ApiError ? err : new ApiError(0, String(err), '')
      opts.onError?.(e)
      return undefined
    }
  }
}

export { api, ApiError, globalMutate as mutate }
