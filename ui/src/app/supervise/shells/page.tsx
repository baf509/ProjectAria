/**
 * ARIA - /supervise/shells index
 *
 * Below lg the layout shows only the list for this segment, so this page is
 * the desktop-only placeholder pane beside it.
 */
import { EmptyState } from '@/components/ui/primitives'

export default function ShellsIndexPage() {
  return (
    <div className="hidden flex-1 place-items-center p-6 lg:grid">
      <EmptyState>Select a shell to open its terminal.</EmptyState>
    </div>
  )
}
