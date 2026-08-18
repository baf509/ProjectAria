'use client'

/**
 * ARIA - the loading/empty/error contract
 *
 * Three rules the old pages got wrong:
 *  - pending renders a skeleton at the FINAL size, so nothing shifts when data
 *    lands and a loading panel is a visible panel rather than a white gap;
 *  - an error with cached data keeps the data and marks it stale (the previous
 *    code blanked the panel, and a poll error also wiped whatever action error
 *    was on screen);
 *  - an error with no data shows FastAPI's `detail`, not "API error 500".
 */
import { ReactNode } from 'react'
import { Resource } from '@/lib/swr'
import { Skeleton, Notice, EmptyState } from './primitives'
import { Button } from './controls'

export function Async<T>({
  r,
  skeletonRows = 3,
  empty,
  isEmpty,
  children,
}: {
  r: Resource<T>
  skeletonRows?: number
  empty?: ReactNode
  isEmpty?: (data: T) => boolean
  children: (data: T) => ReactNode
}) {
  if (r.isLoading) return <Skeleton rows={skeletonRows} />

  if (r.error && r.data === undefined) {
    return (
      <Notice tone="warn">
        <div className="flex flex-wrap items-center gap-3">
          <span className="min-w-0 wrap-anywhere">{r.error.message}</span>
          <Button onClick={() => void r.refresh()} className="ml-auto">
            Retry
          </Button>
        </div>
      </Notice>
    )
  }

  if (r.data === undefined) return <Skeleton rows={skeletonRows} />
  if (empty && isEmpty?.(r.data)) return <EmptyState>{empty}</EmptyState>

  return <>{children(r.data)}</>
}
