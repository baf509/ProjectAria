'use client'

/**
 * ARIA - Service Worker Registration
 *
 * Phase: UI / PWA
 * Purpose: Register /sw.js on the client to enable PWA installability + offline shell.
 */
import { useEffect } from 'react'

export default function ServiceWorkerRegister() {
  useEffect(() => {
    if (typeof window === 'undefined') return
    if (!('serviceWorker' in navigator)) return

    const register = () => {
      navigator.serviceWorker.register('/sw.js').catch((err) => {
        // Non-fatal: app works without the SW.
        console.error('SW registration failed:', err)
      })
    }

    if (document.readyState === 'complete') {
      register()
    } else {
      window.addEventListener('load', register)
      return () => window.removeEventListener('load', register)
    }
  }, [])

  return null
}
