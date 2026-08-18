import { Card, Skeleton } from '@/components/ui/primitives'

/** Skeletons hold the final size — a pending spine is a visible panel, not a white gap. */
export default function Loading() {
  return (
    <div className="flex min-w-0 flex-col gap-gap">
      <Card title="Memory pools">
        <Skeleton rows={2} />
      </Card>
      <Card title="Resident now">
        <Skeleton rows={3} />
      </Card>
      <Card title="Services">
        <Skeleton rows={5} />
      </Card>
    </div>
  )
}
