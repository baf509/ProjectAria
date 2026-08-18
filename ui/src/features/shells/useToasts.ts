'use client'

/**
 * ARIA - toast state hook (shells feature)
 *
 * Same contract as the Inbox's inline copy: push('ok'|'warn', text),
 * auto-dismiss after 6s, manual dismiss by id. Kept as a tiny hook rather than
 * a context because the list and the terminal each own their own action
 * surface — a failed send in the terminal has nothing to say to the list.
 */
import { useState } from 'react'
import type { Toast } from '@/components/ui/controls'

export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([])
  const push = (tone: Toast['tone'], text: string) => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, tone, text }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000)
  }
  return { toasts, push, dismiss: (id: number) => setToasts((t) => t.filter((x) => x.id !== id)) }
}
