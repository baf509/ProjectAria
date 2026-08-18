import { Card, Skeleton } from '@/components/ui/primitives'

/** Segment-level pending state: a visible skeleton, never a white gap. */
export default function Loading() {
  return (
    <Card>
      <Skeleton rows={5} />
    </Card>
  )
}
