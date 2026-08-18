/**
 * Route-segment skeleton: a pending terminal is a visible panel, not a white
 * gap (contract rule 9).
 */
import { Skeleton } from '@/components/ui/primitives'

export default function ShellDetailLoading() {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 p-2.5">
      <Skeleton rows={2} />
      <div className="min-h-0 flex-1">
        <Skeleton rows={10} />
      </div>
    </div>
  )
}
