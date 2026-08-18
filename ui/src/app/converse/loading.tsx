import { Skeleton } from '@/components/ui/primitives'

/** Holds the master-list shape inside the flush shell; never a white gap. */
export default function Loading() {
  return (
    <div className="flex min-h-0 flex-1 flex-col px-safe py-3">
      <Skeleton rows={8} />
    </div>
  )
}
