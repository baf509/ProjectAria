'use client'

/**
 * ARIA - Know: memories
 *
 * SERVER-side search over all ~14,000 memories. The old dashboard fetched the
 * newest 50 and substring-filtered them in the client, so any memory older
 * than the newest 50 looked like it did not exist — "search" was really
 * "filter the first page". Typing now debounces into POST /memories/search;
 * an empty query browses newest-first with real `skip` paging.
 *
 * The retrieval_mode chip exists because BOTH retrieval capabilities are
 * currently OFF on purpose (2026-08-15): every search is served by the
 * mongod-native fallback scan. Degraded recall must look like a stated mode,
 * not like a broken search box.
 */
import { useEffect, useState } from 'react'
import useSWR from 'swr'
import { useResource, useAction, type Resource, ApiError } from '@/lib/swr'
import { K, searchMemories, deleteMemory } from '@/lib/api/endpoints'
import type { Memory, RetrievalCapabilities } from '@/lib/api/types'
import { Card, Chip, Notice, Text, EmptyState, KeyValue } from '@/components/ui/primitives'
import { Button, ConfirmButton, Disclosure, Input, Toasts } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Stack, Cluster } from '@/components/layout'
import { relativeTime } from '@/lib/time'
import { useKnowStats } from '@/features/know/knowStatus'
import { useToasts } from '@/features/know/useToasts'

const PAGE = 50

function useDebounced<T>(value: T, ms = 400): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return debounced
}

/**
 * The search endpoint is POST-only (no GET variant exists), and the shared
 * `useResource` only speaks the GET fetcher. This wraps the typed caller from
 * endpoints.ts in SWR directly — same cache, dedupe and keepPreviousData —
 * and returns the Resource shape so <Async> works unchanged. If useResource
 * ever grows a custom-fetcher option, fold this into it.
 */
function useMemorySearch(query: string, limit: number): Resource<Memory[]> {
  const res = useSWR<Memory[], ApiError>(
    query ? (['know-memory-search', query, limit] as const) : null,
    ([, q, l]: readonly [string, string, number]) => searchMemories(q, l),
    { keepPreviousData: true, revalidateOnFocus: false, dedupingInterval: 2_000 }
  )
  return {
    data: res.data,
    error: res.error,
    isLoading: res.isLoading && res.data === undefined,
    stale: res.error !== undefined && res.data !== undefined,
    updatedAt: res.data !== undefined ? Date.now() : undefined,
    refresh: () => res.mutate(),
    mutate: res.mutate,
  }
}

function MemoryRow({ memory, onDeleted, onError }: { memory: Memory; onDeleted: () => void; onError: (t: string) => void }) {
  const run = useAction()
  return (
    <li className="cv-auto border-b border-line py-2 last:border-b-0">
      <Disclosure
        summary={
          <span className="flex min-w-0 flex-col gap-1">
            <span className="flex min-w-0 flex-wrap items-center gap-1.5">
              <Chip tone="accent">{memory.content_type || 'memory'}</Chip>
              {(memory.categories ?? []).slice(0, 2).map((c) => (
                <Chip key={c}>{c}</Chip>
              ))}
              <span className="ml-auto shrink-0 text-micro text-ink-faint">{relativeTime(memory.created_at)}</span>
            </span>
            <span className="line-clamp-2 min-w-0 wrap-anywhere font-sans text-prose text-ink">{memory.content}</span>
          </span>
        }
      >
        <Stack gap="sm">
          <Text>{memory.content}</Text>
          <KeyValue
            layout="stack"
            items={[
              { k: 'Importance', v: memory.importance ?? '—', kind: 'num' },
              { k: 'Confidence', v: memory.confidence ?? '—', kind: 'num' },
              { k: 'Source', v: memory.source?.type || 'unknown', kind: 'ident' },
              ...(memory.categories?.length
                ? [{ k: 'Categories', v: memory.categories.join(' · '), kind: 'prose' as const }]
                : []),
            ]}
          />
          <Cluster>
            <ConfirmButton
              label="Delete"
              onConfirm={async () => {
                const ok = await run(() => deleteMemory(memory.id), {
                  invalidate: ['/memories'],
                  onError: (e) => onError(e.message),
                })
                if (ok !== undefined) onDeleted()
              }}
            />
          </Cluster>
        </Stack>
      </Disclosure>
    </li>
  )
}

export default function MemoriesPage() {
  const toasts = useToasts()
  const [input, setInput] = useState('')
  const [skip, setSkip] = useState(0)
  const [searchLimit, setSearchLimit] = useState(20)
  const query = useDebounced(input.trim())

  // Reset paging whenever the query flips between browse and search.
  useEffect(() => {
    setSkip(0)
    setSearchLimit(20)
  }, [query])

  const caps = useResource<RetrievalCapabilities>(K.capabilities, { tier: 'slow' })
  const browse = useResource<Memory[]>(K.memoriesPage(PAGE, skip), { tier: 'lazy', enabled: !query })
  const search = useMemorySearch(query, searchLimit)
  const results = query ? search : browse

  const mode = caps.data?.retrieval_mode
  const degraded = mode !== undefined && mode !== 'hybrid'
  const pending = caps.data?.backfill?.pending?.memories

  useKnowStats([
    { label: 'SHOWN', value: results.data?.length ?? '—' },
    ...(mode ? [{ label: 'RECALL', value: mode, tone: degraded ? ('warn' as const) : ('ok' as const) }] : []),
    ...(pending ? [{ label: 'QUEUED', value: pending }] : []),
  ])

  return (
    <>
      <Stack>
        <Card
          title="Memories"
          hint={query ? 'server-side search' : 'newest first'}
          actions={
            mode && (
              <Chip tone={degraded ? 'warn' : 'ok'}>recall: {mode}</Chip>
            )
          }
        >
          <Stack gap="sm">
            <Input
              type="search"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Search all memories…"
              aria-label="Search memories"
              className="coarse:text-title"
            />
            {degraded && (
              <Notice tone="info">
                Retrieval is in <b>{mode}</b> mode: embeddings and mongot search are switched off on purpose
                (2026-08-15), so recall runs on the mongod-native scan. Results are degraded by design — not a bug.
                {pending ? ` ${pending.toLocaleString()} memories are queued for re-embedding.` : ''}
              </Notice>
            )}
            <Async
              r={results}
              skeletonRows={6}
              isEmpty={(d) => d.length === 0}
              empty={query ? 'No memories matched that search.' : 'No memories stored yet.'}
            >
              {(items) => (
                <ul className="m-0 list-none p-0">
                  {items.map((m) => (
                    <MemoryRow key={m.id} memory={m} onDeleted={() => toasts.ok('Memory deleted')} onError={toasts.warn} />
                  ))}
                </ul>
              )}
            </Async>
            {query ? (
              (results.data?.length ?? 0) >= searchLimit && (
                <Button onClick={() => setSearchLimit((l) => Math.min(l * 2 + 10, 200))} className="self-start">
                  More results
                </Button>
              )
            ) : (
              <Cluster>
                <Button disabled={skip === 0} onClick={() => setSkip((s) => Math.max(0, s - PAGE))}>
                  Newer
                </Button>
                <Button
                  disabled={(browse.data?.length ?? 0) < PAGE}
                  onClick={() => setSkip((s) => s + PAGE)}
                >
                  Older
                </Button>
                {skip > 0 && (
                  <span className="tnum text-micro text-ink-faint">
                    {skip + 1}–{skip + (browse.data?.length ?? 0)}
                  </span>
                )}
              </Cluster>
            )}
          </Stack>
        </Card>
      </Stack>
      <Toasts toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </>
  )
}
