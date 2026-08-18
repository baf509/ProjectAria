'use client'

/**
 * Route-level error boundary. Without one, an unhandled render error is Next's
 * white "Application error" screen — the same visual as an empty page, which is
 * exactly the ambiguity this rebuild is removing.
 */
import { useEffect } from 'react'

export default function Error({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error(error)
  }, [error])

  return (
    <div className="min-h-dvh bg-ground p-gutter font-mono text-body text-ink">
      <div className="mx-auto max-w-prose rounded border border-gone bg-gone/10 p-4">
        <h1 className="m-0 text-title">Something broke on this screen</h1>
        <p className="mt-2 font-sans text-prose wrap-anywhere text-ink-dim">{error.message}</p>
        <button
          onClick={reset}
          className="mt-3 min-h-control rounded-sm border border-line px-3 text-micro uppercase tracking-[0.08em]"
        >
          Try again
        </button>
      </div>
    </div>
  )
}
