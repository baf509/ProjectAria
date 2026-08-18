import { Card, Skeleton } from '@/components/ui/primitives'

export default function Loading() {
  return (
    <div className="flex min-w-0 flex-col gap-gap">
      <Card title="Model">
        <Skeleton rows={8} />
      </Card>
      <Card title="Memory fit">
        <Skeleton rows={2} />
      </Card>
    </div>
  )
}
