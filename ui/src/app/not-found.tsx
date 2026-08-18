import Link from 'next/link'

export default function NotFound() {
  return (
    <div className="min-h-dvh bg-ground p-gutter font-mono text-body text-ink">
      <div className="mx-auto max-w-prose rounded border border-line bg-panel p-4">
        <h1 className="m-0 text-title">No such screen</h1>
        <p className="mt-2 font-sans text-prose text-ink-dim">
          The URL does not match any area.{' '}
          <Link href="/inbox" className="text-accent underline underline-offset-2">
            Go to the Inbox
          </Link>
          .
        </p>
      </div>
    </div>
  )
}
