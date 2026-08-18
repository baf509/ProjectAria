'use client'

/**
 * ARIA - Know toasts hook
 *
 * Same contract as the Inbox reference: action feedback is a toast, not a
 * page-top banner that scrolls away (the old dashboard's `statusMessage` strip
 * sat above the tabs and was off-screen by the time any list action ran).
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
  return {
    toasts,
    ok: (text: string) => push('ok', text),
    warn: (text: string) => push('warn', text),
    dismiss: (id: number) => setToasts((t) => t.filter((x) => x.id !== id)),
  }
}
