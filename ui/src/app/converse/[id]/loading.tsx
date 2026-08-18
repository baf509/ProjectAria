import { Skeleton } from '@/components/ui/primitives'

/** Thread-shaped skeleton: header row, message area, composer strip. */
export default function Loading() {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="h-control shrink-0 border-b border-line" />
      <div className="min-h-0 flex-1 px-safe py-3">
        <div className="mx-auto w-full max-w-3xl">
          <Skeleton rows={6} />
        </div>
      </div>
      <div className="h-control shrink-0 border-t border-line" />
    </div>
  )
}
