'use client'

/**
 * ARIA - conversation list (master)
 *
 * One component, three frames: the lg sidebar rail, the phone's full-screen
 * master at /converse, and the in-thread switcher Sheet. All three read the
 * same SWR key, so mounting more than one costs no extra requests.
 *
 * Two measured defects this replaces:
 *  - "+ New Chat" silently did nothing: createConversation without an
 *    agent_slug resolves to the deliberately-disabled `aria` agent and the API
 *    refuses with a 400 the old page swallowed. New here = pick an enabled
 *    agent (Sheet), then create, and any refusal is surfaced.
 *  - Selection was component state, so it did not survive reload and Back did
 *    nothing. Rows are real links to /converse/[id].
 */
import { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { useRouter, useSelectedLayoutSegment } from 'next/navigation'
import { Plus, Trash2 } from 'lucide-react'
import { useResource, useAction } from '@/lib/swr'
import { K, conversationSearchKey, createConversation, deleteConversation } from '@/lib/api/endpoints'
import type { Agent, ConversationListEntry } from '@/lib/api/types'
import { EmptyState, Notice } from '@/components/ui/primitives'
import { Async } from '@/components/ui/Async'
import { Button, IconButton, Input, Sheet } from '@/components/ui/controls'
import { relativeTime } from '@/lib/time'
import { agentIcon, enabledAgents } from './agentIcon'

function DeleteButton({ onDelete }: { onDelete: () => Promise<void> }) {
  /**
   * Two-tap confirm. window.confirm is suppressed by mobile browsers, so the
   * arm/confirm has to be in the page.
   *
   * The first cut armed by swapping a 16px trash icon for a 16px check and
   * tinting it — which Ben read, correctly, as "nothing is happening": the only
   * feedback was an icon most people do not look at, and the 4s timer then
   * disarmed it silently, so a considered second click did nothing either.
   * Armed state now says the word DELETE? in a danger-toned button (the same
   * thing ConfirmButton does, and the reason that one works), and the window is
   * long enough to read it.
   */
  const [armed, setArmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  async function handle() {
    if (!armed) {
      setArmed(true)
      timer.current = setTimeout(() => setArmed(false), 8000)
      return
    }
    if (timer.current) clearTimeout(timer.current)
    setArmed(false)
    setBusy(true)
    try {
      await onDelete()
    } finally {
      setBusy(false)
    }
  }

  if (armed) {
    return (
      <Button variant="danger" busy={busy} onClick={handle} className="shrink-0">
        Delete?
      </Button>
    )
  }

  return (
    <IconButton label="Delete conversation" disabled={busy} onClick={handle}>
      <Trash2 size={16} aria-hidden="true" />
    </IconButton>
  )
}

export function ConversationList({
  frame,
  onNavigate,
}: {
  /** rail/page own their scroll; sheet flows inside the Sheet's scroller. */
  frame: 'rail' | 'page' | 'sheet'
  onNavigate?: () => void
}) {
  const router = useRouter()
  const selected = useSelectedLayoutSegment()
  const run = useAction()

  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), 300)
    return () => clearTimeout(t)
  }, [query])

  const list = useResource<ConversationListEntry[]>(
    debounced ? conversationSearchKey(debounced) : K.conversations(50),
    { tier: 'lazy' }
  )
  const agents = useResource<Agent[]>(K.agents, { tier: 'lazy' })

  const [pickerOpen, setPickerOpen] = useState(false)
  const [creating, setCreating] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function createWith(agent: Agent) {
    if (!agent.slug) return
    setCreating(agent.slug)
    setError(null)
    const convo = await run(() => createConversation({ title: 'New Chat', agent_slug: agent.slug! }), {
      invalidate: ['/conversations'],
      onError: (e) => setError(`Could not create conversation: ${e.message}`),
    })
    setCreating(null)
    if (convo) {
      setPickerOpen(false)
      onNavigate?.()
      router.push(`/converse/${convo.id}`)
    }
  }

  async function remove(id: string) {
    await run(() => deleteConversation(id), {
      invalidate: ['/conversations'],
      onError: (e) => setError(`Delete failed: ${e.message}`),
    })
    // Deleting the open conversation leaves a dead detail route — go back to
    // the master rather than letting the thread 404 on its next revalidate.
    if (selected === id) router.push('/converse')
  }

  const scroll = frame === 'sheet' ? '' : 'min-h-0 flex-1 overflow-y-auto overscroll-contain'

  return (
    <div className={`flex min-h-0 min-w-0 flex-col ${frame === 'sheet' ? '' : 'flex-1'}`}>
      <div className="flex shrink-0 items-center gap-2 pb-2">
        <Input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search conversations"
          aria-label="Search conversations"
        />
        <Button variant="primary" className="shrink-0" onClick={() => setPickerOpen(true)} aria-haspopup="dialog">
          <Plus size={15} aria-hidden="true" />
          New
        </Button>
      </div>

      {error && (
        <Notice tone="warn" className="mb-2 shrink-0">
          {error}
        </Notice>
      )}

      <div className={scroll}>
        <Async
          r={list}
          skeletonRows={6}
          empty={debounced ? 'No conversations match.' : 'No conversations yet — start one with New.'}
          isEmpty={(rows) => rows.length === 0}
        >
          {(rows) => (
          <ul className="m-0 flex list-none flex-col p-0">
            {rows.map((c) => {
              const active = selected === c.id
              return (
                <li key={c.id} className="flex min-w-0 items-center gap-1 border-b border-line last:border-b-0">
                  <Link
                    href={`/converse/${c.id}`}
                    onClick={onNavigate}
                    aria-current={active ? 'page' : undefined}
                    className={`flex min-h-row min-w-0 flex-1 flex-col justify-center gap-0.5 rounded-sm px-2 py-1.5 ${
                      active ? 'border-l-2 border-accent bg-panel-2' : 'border-l-2 border-transparent hover:bg-panel-2'
                    }`}
                  >
                    <span className="line-clamp-2 min-w-0 font-sans text-body text-ink">
                      {c.title || 'Untitled'}
                    </span>
                    <span className="tnum flex min-w-0 items-center gap-2 text-micro text-ink-faint">
                      {relativeTime(c.updated_at)}
                      {c.stats?.message_count !== undefined && <span>{c.stats.message_count} msg</span>}
                    </span>
                  </Link>
                  <DeleteButton onDelete={() => remove(c.id)} />
                </li>
              )
            })}
          </ul>
          )}
        </Async>
      </div>

      <Sheet open={pickerOpen} onClose={() => setPickerOpen(false)} title="New conversation">
        {/* Async, not a bare read: with agents.data still loading (or failed),
            enabledAgents(undefined)=[] rendered "No enabled agents — enable one
            under Know" for what was actually a fetch failure. */}
        <Async r={agents} skeletonRows={3}>
          {(rows) => (
            <AgentPicker
              agents={enabledAgents(rows)}
              busySlug={creating}
              onPick={(a) => void createWith(a)}
              hint="Pick the agent this conversation talks to. The default ARIA persona is disabled by design — Hermes is the front door; these are work agents."
            />
          )}
        </Async>
      </Sheet>
    </div>
  )
}

/** Shared by "New conversation" and the thread's mode switcher. */
export function AgentPicker({
  agents,
  busySlug,
  onPick,
  currentId,
  hint,
}: {
  agents: Agent[]
  busySlug?: string | null
  onPick: (agent: Agent) => void
  currentId?: string
  hint?: string
}) {
  if (agents.length === 0) {
    return <EmptyState>No enabled agents. Enable one under Know → Agents first.</EmptyState>
  }
  return (
    <div className="flex min-w-0 flex-col gap-2">
      {hint && <p className="m-0 font-sans text-label text-ink-faint">{hint}</p>}
      <ul className="m-0 flex list-none flex-col p-0">
        {agents.map((a) => {
          const Icon = agentIcon(a)
          const current = currentId !== undefined && a.id === currentId
          return (
            <li key={a.id} className="border-b border-line last:border-b-0">
              <button
                onClick={() => onPick(a)}
                disabled={busySlug !== null && busySlug !== undefined}
                aria-current={current ? 'true' : undefined}
                className={`flex min-h-control w-full min-w-0 items-center gap-3 rounded-sm px-2 py-2 text-left hover:bg-panel-2 disabled:opacity-40 ${
                  current ? 'bg-panel-2' : ''
                }`}
              >
                <Icon size={17} strokeWidth={1.75} aria-hidden="true" className="shrink-0 text-ink-dim" />
                <span className="min-w-0 flex-1">
                  <span className="block text-body text-ink">
                    {a.name}
                    {current && <span className="ml-2 text-micro uppercase text-accent">current</span>}
                  </span>
                  {a.description && (
                    <span className="line-clamp-2 block font-sans text-label text-ink-faint">{a.description}</span>
                  )}
                </span>
                {busySlug === a.slug && (
                  <span
                    aria-hidden="true"
                    className="h-3 w-3 shrink-0 animate-spin rounded-full border border-current border-t-transparent"
                  />
                )}
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
