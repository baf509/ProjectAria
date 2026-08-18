'use client'

/**
 * Which build is actually running.
 *
 * The deployed image was six days behind source when the rebuild audit ran and
 * nothing on the page said so. `make ui-deploy` refuses to finish unless this
 * matches HEAD.
 */
import useSWR from 'swr'
import { api } from '@/lib/http'

type Build = { sha: string; date: string; branch?: string }

export function BuildStamp() {
  const { data } = useSWR<Build>('/build', () => api<Build>('/build', { base: '/api' }), {
    revalidateOnFocus: false,
    refreshInterval: 0,
  })
  if (!data) return null
  return (
    <p className="tnum m-0 text-micro text-ink-faint">
      build <span className="text-ink-dim">{data.sha}</span>
      {data.date ? ` · ${data.date.slice(0, 10)}` : ''}
    </p>
  )
}
