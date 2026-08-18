'use client'

/**
 * /converse index. Below lg this IS the master screen (the layout's rail is
 * hidden there); on the laptop the rail already shows the list, so the index
 * is just a pick-something hint.
 */
import { ConversationList } from '@/features/converse/ConversationList'
import { EmptyState } from '@/components/ui/primitives'

export default function ConverseIndexPage() {
  return (
    <>
      {/* Bottom padding clears the fixed tab bar — flush pages pad for it themselves. */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col px-safe py-3 pb-[calc(var(--tabbar-h)+var(--sab)+0.75rem)] lg:hidden">
        <ConversationList frame="page" />
      </div>
      <div className="hidden min-h-0 flex-1 place-items-center lg:grid">
        <EmptyState>Select a conversation, or start one with New.</EmptyState>
      </div>
    </>
  )
}
