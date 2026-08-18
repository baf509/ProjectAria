'use client'

import { useEffect } from 'react'

/**
 * Publishes the visual viewport height as `--vvh`.
 *
 * `100dvh` handles the URL bar but NOT the on-screen keyboard on iOS Safari,
 * which ignores `interactive-widget` — so a flush surface (chat thread,
 * terminal) would put its composer underneath the keyboard. visualViewport is
 * the only signal that moves when the keyboard opens.
 */
export function useVisualViewport() {
  useEffect(() => {
    const vv = typeof window !== 'undefined' ? window.visualViewport : undefined
    if (!vv) return
    const apply = () => {
      document.documentElement.style.setProperty('--vvh', `${vv.height}px`)
    }
    apply()
    vv.addEventListener('resize', apply)
    vv.addEventListener('scroll', apply)
    return () => {
      vv.removeEventListener('resize', apply)
      vv.removeEventListener('scroll', apply)
    }
  }, [])
}
