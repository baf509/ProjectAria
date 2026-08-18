import { Card, Skeleton } from '@/components/ui/primitives'

export default function Loading() {
  return (
    <div className="flex min-w-0 flex-col gap-gap">
      <Card title="Service">
        <Skeleton rows={6} />
      </Card>
    </div>
  )
}
