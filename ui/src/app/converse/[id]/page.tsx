'use client'

/** The flush thread. All behaviour lives in features/converse/Thread. */
import { Thread } from '@/features/converse/Thread'

export default function ConversationPage({ params }: { params: { id: string } }) {
  return <Thread id={params.id} />
}
