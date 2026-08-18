'use client'

/**
 * ARIA - service worker registration + update prompt
 *
 * Two things the previous version got wrong: it swallowed the "not a secure
 * context" case silently (so on the http origin the PWA was, in practice, a
 * bookmark and nobody knew), and the worker itself claimed clients on install,
 * which is a silent mid-session takeover. Now: registration is skipped loudly
 * in dev-console terms, and a new build asks before reloading.
 */
import { useEffect, useState } from 'react'

export default function ServiceWorkerRegister() {
  const [waiting, setWaiting] = useState<ServiceWorker | null>(null)

  useEffect(() => {
    if (typeof window === 'undefined') return
    if (!('serviceWorker' in navigator)) {
      if (!window.isSecureContext) {
        console.info(
          '[aria] service worker skipped: not a secure context. Serve the UI over https (tailscale serve --https) for offline support and update prompts.'
        )
      }
      return
    }

    let cancelled = false

    const register = async () => {
      try {
        const reg = await navigator.serviceWorker.register('/sw.js')
        if (cancelled) return
        if (reg.waiting) setWaiting(reg.waiting)
        reg.addEventListener('updatefound', () => {
          const next = reg.installing
          if (!next) return
          next.addEventListener('statechange', () => {
            if (next.state === 'installed' && navigator.serviceWorker.controller) setWaiting(next)
          })
        })
      } catch (err) {
        console.error('[aria] service worker registration failed:', err)
      }
    }

    // Reload ONLY when an update we prompted for takes over — never on the
    // first install.
    //
    // Measured 2026-08-17: with an unconditional reload here, every first visit
    // loaded the page TWICE and fired every API request twice. A worker that
    // calls clients.claim() on activate (as the pre-rebuild one did) takes
    // control of the page that just registered it, which fires controllerchange
    // immediately — so "the controller changed" is not the same question as
    // "a new version is ready". If there was no controller when we started,
    // this is the initial claim and there is nothing to reload for.
    const hadController = Boolean(navigator.serviceWorker.controller)
    let reloading = false
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!hadController || reloading) return
      reloading = true
      window.location.reload()
    })

    if (document.readyState === 'complete') void register()
    else window.addEventListener('load', register, { once: true })

    return () => {
      cancelled = true
    }
  }, [])

  if (!waiting) return null

  return (
    <div className="fixed inset-x-0 bottom-[calc(var(--tabbar-h)+var(--sab)+0.5rem)] z-toast flex justify-center px-gutter lg:bottom-4">
      <button
        onClick={() => waiting.postMessage('SKIP_WAITING')}
        className="w-full max-w-md rounded-sm border border-accent bg-accent/10 px-3 py-2 text-left font-sans text-prose text-ink shadow-lg"
      >
        A new build of ARIA is ready — tap to reload.
      </button>
    </div>
  )
}
