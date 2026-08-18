'use client'

/**
 * ARIA - retire a project
 *
 * Ending a project is normally avoided because deleting the row feels like
 * discarding the only record of it. So this is not a delete: it distils the
 * project's transcripts into long-term memory FIRST, verifies they landed, and
 * only then removes the board row. Scrollback, coding sessions and every memory
 * the extraction workers already minted are kept — they have their own
 * retention and retiring a project is not a licence to erase them.
 *
 * Two steps by construction: the preview is a real server-side dry run (same
 * code path, nothing written), so what you confirm is what will happen rather
 * than a guess rendered in the client.
 */
import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { Card, Notice, Text, KeyValue } from '@/components/ui/primitives'
import { Button, ConfirmButton, Disclosure } from '@/components/ui/controls'
import { Stack, Cluster } from '@/components/layout'
import { useAction } from '@/lib/swr'
import { retireProject, type RetireReport } from '@/lib/api/endpoints'

export function RetireProject({ slug, name }: { slug: string; name: string }) {
  const run = useAction()
  const router = useRouter()
  const [preview, setPreview] = useState<RetireReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<RetireReport | null>(null)

  async function loadPreview() {
    setBusy(true)
    setError(null)
    const r = await run(() => retireProject(slug, true), { onError: (e) => setError(e.message) })
    setBusy(false)
    if (r) setPreview(r)
  }

  async function commit() {
    setBusy(true)
    setError(null)
    const r = await run(() => retireProject(slug, false), {
      invalidate: ['/projects'],
      onError: (e) => setError(e.message),
    })
    setBusy(false)
    if (r) {
      setDone(r)
      // The detail route no longer has a project behind it.
      setTimeout(() => router.push('/supervise'), 1200)
    }
  }

  if (done) {
    return (
      <Card title="Retired">
        <Notice tone="ok">
          {name} was retired. {done.memories_written?.length ?? 0} memory/memories written; scrollback and
          sessions kept. Returning to Supervise…
        </Notice>
      </Card>
    )
  }

  return (
    <Card title="Retire this project" hint="transcripts to memory, then remove">
      <Stack gap="sm">
        <Text>
          Distils this project&apos;s transcripts into long-term memory, then removes it from the board.
          Scrollback, coding sessions and existing memories are kept. Refused while a session is running
          or a shell is active.
        </Text>

        {error && <Notice tone="warn">{error}</Notice>}

        {!preview ? (
          <Button busy={busy} onClick={loadPreview} className="self-start">
            Preview retirement
          </Button>
        ) : (
          <>
            <KeyValue
              layout="stack"
              items={[
                { k: 'Shells read', v: preview.shells?.length ? preview.shells.join(', ') : 'none', kind: 'ident' },
                { k: 'Coding sessions', v: String(preview.sessions ?? 0), kind: 'num' },
                { k: 'Transcript scanned', v: `${(preview.transcript_chars ?? 0).toLocaleString()} chars`, kind: 'num' },
              ]}
            />
            <Disclosure summary={<span className="text-body text-ink-dim">What will be written to memory</span>}>
              <pre className="m-0 wrap-anywhere rounded-sm border border-line bg-panel-2 p-2.5 font-mono text-micro">
                {preview.record}
              </pre>
            </Disclosure>
            <Cluster>
              <ConfirmButton
                label="Retire project"
                confirmLabel={`Retire ${name} — remove it?`}
                onConfirm={commit}
                disabled={busy}
              />
              <Button onClick={() => setPreview(null)} disabled={busy}>
                Cancel
              </Button>
            </Cluster>
          </>
        )}
      </Stack>
    </Card>
  )
}
