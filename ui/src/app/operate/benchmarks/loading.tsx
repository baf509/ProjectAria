import { Card, Skeleton } from '@/components/ui/primitives'

/** Skeletons hold the final size, so nothing shifts and no white gap appears. */
export default function Loading() {
  return (
    <div className="mx-auto flex w-full max-w-page flex-col gap-gap px-safe py-3">
      <Card title="Launch">
        <Skeleton rows={5} />
      </Card>
      <Card title="Runs">
        <Skeleton rows={6} />
      </Card>
    </div>
  )
}
