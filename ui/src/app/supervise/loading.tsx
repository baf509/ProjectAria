import { Card, Skeleton } from '@/components/ui/primitives'

/** Skeletons hold the final size, so nothing shifts and no white gap appears. */
export default function Loading() {
  return (
    <div className="mx-auto flex w-full max-w-page flex-col gap-gap px-safe py-3">
      <Card title="Fleet">
        <Skeleton rows={1} />
      </Card>
      <Card title="Projects">
        <Skeleton rows={5} />
      </Card>
    </div>
  )
}
