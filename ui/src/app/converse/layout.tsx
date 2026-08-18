'use client'

/**
 * ARIA - /converse layout: master list + flush thread
 *
 * Master/detail is ROUTING, not component state (contract rule 8): the list is
 * this layout, the thread is [id]/page.tsx, so the selection survives reload
 * and Back works — the old /chat kept the current conversation in useState,
 * which the installed-PWA (no browser chrome) could never navigate out of.
 *
 * `<AppShell flush>` owns the height chain at every width. The old flush chain
 * was gated behind `lg:` so the document itself scrolled on phones — the
 * mechanism behind scrollIntoView dragging the page ~2000px.
 *
 * Below lg the sidebar is hidden entirely (the /converse index page shows the
 * same list full-screen, and the thread header's title button opens it as a
 * Sheet) — a persistent 16rem rail would leave a 23rem thread on a 390px phone.
 */
import { ReactNode } from 'react'
import { useSelectedLayoutSegment } from 'next/navigation'
import { AppShell } from '@/components/shell/AppShell'
import { ConversationList } from '@/features/converse/ConversationList'

export default function ConverseLayout({ children }: { children: ReactNode }) {
  const segment = useSelectedLayoutSegment()
  const detailOpen = segment !== null

  return (
    <AppShell flush back={detailOpen ? { href: '/converse', label: 'Conversations' } : undefined}>
      <div className="flex min-h-0 min-w-0 flex-1">
        <aside className="hidden w-72 shrink-0 border-r border-line lg:flex lg:min-h-0 lg:flex-col lg:p-3">
          <ConversationList frame="rail" />
        </aside>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">{children}</div>
      </div>
    </AppShell>
  )
}
