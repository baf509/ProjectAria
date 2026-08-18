import Link from 'next/link'

export const metadata = { title: 'Offline' }

/** Served by the service worker when a navigation cannot reach the network. */
export default function Offline() {
  return (
    <div className="min-h-dvh bg-ground p-gutter font-mono text-body text-ink">
      <div className="mx-auto max-w-prose rounded border border-line bg-panel p-4">
        <h1 className="m-0 text-title">Offline</h1>
        <p className="mt-2 font-sans text-prose text-ink-dim">
          ARIA is on the tailnet — this device cannot reach it right now. Cached screens still open;
          live data resumes automatically.
        </p>
        <Link href="/inbox" className="mt-3 inline-block text-accent underline underline-offset-2">
          Back to the Inbox
        </Link>
      </div>
    </div>
  )
}
