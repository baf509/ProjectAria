'use client'

import { useEffect, useState } from 'react'
import { K } from '@/lib/api/endpoints'
import type { ShellScreenPayload } from '@/lib/api/types'
import { openSse } from '@/lib/stream'

/** One visible subscription; reconnect gets the current screen, never a replay. */
export function useLiveScreen(name: string, enabled: boolean) {
  const [data, setData] = useState<ShellScreenPayload>()
  const [error, setError] = useState<Error>()
  useEffect(() => {
    setData(undefined)
    setError(undefined)
    if (!enabled) return
    let stopped = false
    let controller: AbortController | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    let retry = 500
    const isHidden = () => document.visibilityState === 'hidden'

    const connect = async () => {
      if (stopped || isHidden()) return
      const current = new AbortController()
      controller = current
      try {
        for await (const frame of openSse(K.shellScreenStream(name), { signal: current.signal })) {
          if (stopped || current.signal.aborted) return
          if (frame.event !== 'screen') continue
          const next = JSON.parse(frame.data) as ShellScreenPayload & { error?: string }
          if (next.error) {
            setError(new Error(next.error))
          } else {
            setData(previous => previous?.screen === next.screen ? previous : next)
            setError(undefined)
          }
          retry = 500
        }
        if (!current.signal.aborted) setError(new Error('Reconnecting to shell…'))
      } catch (err) {
        if (!current.signal.aborted) setError(err instanceof Error ? err : new Error(String(err)))
      } finally {
        if (!stopped && !current.signal.aborted && !isHidden()) {
          timer = setTimeout(connect, retry)
          retry = Math.min(8000, retry * 2)
        }
      }
    }
    const visibility = () => {
      controller?.abort()
      clearTimeout(timer)
      if (document.visibilityState === 'visible') void connect()
    }
    document.addEventListener('visibilitychange', visibility)
    void connect()
    return () => {
      stopped = true
      controller?.abort()
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [name, enabled])
  return { data, error }
}
