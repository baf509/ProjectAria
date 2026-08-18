'use client'

/**
 * ARIA - conversation thread (flush)
 *
 * The streaming surface, rebuilt around the four measured defects of the old
 * /chat page:
 *
 *  1. O(n^2) token handling: every SSE token did `chunks.join('')` + a state
 *     write, re-rendering the ENTIRE history per token. Tokens now land in a
 *     plain string buffer flushed once per animation frame, and only
 *     <StreamingRow> subscribes — history rows are memoized and never
 *     re-render during a stream.
 *  2. `scrollIntoView({smooth})` on every token dragged the overflow-hidden
 *     document by ~2000px (the flush height chain was lg-only). Scrolling here
 *     targets the message PANE, only while the reader is pinned near the
 *     bottom (tracked from real scroll events, so scrolling up to re-read
 *     stops the auto-follow), and never smoothly while streaming.
 *  3. The retry loop RE-POSTED the user message on any error; the server has
 *     no idempotency, so each retry was a brand-new turn and a second
 *     generation. Now: one silent retry ONLY for a network failure before the
 *     first byte; anything later surfaces the error and leaves retrying to a
 *     human (before-first-byte failures put the text back in the composer).
 *  4. Leaving the page kept the server generating on DS4's single pi slot. An
 *     AbortController is threaded through openSse: Send becomes Stop, and
 *     unmount/navigation aborts the fetch, which the BFF proxy propagates
 *     upstream.
 */
import {
  memo,
  startTransition,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'
import { ChevronDown, Send, Square, Wrench } from 'lucide-react'
import { useResource, useAction, mutate as swrMutate } from '@/lib/swr'
import { K, switchConversationMode } from '@/lib/api/endpoints'
import type { Agent, ChatMessage, ChatStreamData, ConversationDetail } from '@/lib/api/types'
import { openSse } from '@/lib/stream'
import { Chip, EmptyState, Notice } from '@/components/ui/primitives'
import { Async } from '@/components/ui/Async'
import { Button, Sheet, Textarea, Toasts, type Toast } from '@/components/ui/controls'
import { Markdown } from './Markdown'
import { ConversationList, AgentPicker } from './ConversationList'
import { agentIcon, agentById, enabledAgents } from './agentIcon'

/* ------------------------------------------------------------ stream store */

/**
 * Token accumulator living OUTSIDE React. `push` is called per SSE chunk;
 * subscribers are notified at most once per animation frame, so a thousand
 * chunks cost a thousand string appends but only ~frame-rate renders.
 */
type StreamStore = {
  readonly text: string
  readonly tools: string[]
  push: (t: string) => void
  pushTool: (name: string) => void
  reset: () => void
  subscribe: (l: () => void) => () => void
}

function createStreamStore(): StreamStore {
  let text = ''
  let tools: string[] = []
  let raf: number | null = null
  const listeners = new Set<() => void>()
  const notify = () => {
    if (raf !== null) return
    raf = requestAnimationFrame(() => {
      raf = null
      listeners.forEach((l) => l())
    })
  }
  return {
    get text() {
      return text
    },
    get tools() {
      return tools
    },
    push(t) {
      text += t
      notify()
    },
    pushTool(name) {
      tools = [...tools, name]
      notify()
    },
    reset() {
      text = ''
      tools = []
      if (raf !== null) {
        cancelAnimationFrame(raf)
        raf = null
      }
      listeners.forEach((l) => l())
    },
    subscribe(l) {
      listeners.add(l)
      return () => listeners.delete(l)
    },
  }
}

/* ------------------------------------------------------------ message rows */

/** Memoized so streaming state changes in the parent never touch history. */
const MessageRow = memo(function MessageRow({ msg }: { msg: ChatMessage }) {
  const tools = msg.tool_calls?.length ?? 0
  if (msg.role === 'user') {
    return (
      <div className="min-w-0 rounded-sm border-l-2 border-accent bg-panel-2 px-3 py-2">
        <p className="m-0 whitespace-pre-wrap font-sans text-prose text-ink">{msg.content}</p>
      </div>
    )
  }
  return (
    <div className="min-w-0">
      <Markdown text={msg.content} />
      {tools > 0 && (
        <Chip className="mt-1.5">
          <Wrench size={11} aria-hidden="true" />
          {tools} tool{tools === 1 ? '' : 's'}
        </Chip>
      )}
    </div>
  )
})

/**
 * The ONLY component that re-renders per flush. Kept separate so the frame-rate
 * updates stop at its boundary. Streaming text renders as pre-wrap plain text,
 * not markdown: parsing a half-open code fence per frame flickers, and the
 * finished message is re-rendered as markdown from the persisted conversation.
 */
function StreamingRow({ store, onGrow }: { store: StreamStore; onGrow: () => void }) {
  const [, setTick] = useState(0)
  useEffect(
    () =>
      store.subscribe(() => {
        startTransition(() => setTick((t) => t + 1))
      }),
    [store]
  )
  const text = store.text
  const tools = store.tools
  useLayoutEffect(() => {
    onGrow()
  }, [text.length, tools.length, onGrow])

  return (
    <div className="min-w-0">
      {tools.length > 0 && (
        <div className="mb-1.5 flex min-w-0 flex-wrap gap-1.5">
          {tools.map((t, i) => (
            <Chip key={i}>
              <Wrench size={11} aria-hidden="true" />
              {t}
            </Chip>
          ))}
        </div>
      )}
      <p className="m-0 min-w-0 whitespace-pre-wrap font-sans text-prose text-ink">
        {text}
        <span aria-hidden="true" className="ml-0.5 inline-block h-[1em] w-[2px] translate-y-[2px] animate-pulse bg-accent" />
      </p>
    </div>
  )
}


/** openSse throws the raw response body on a non-2xx; unwrap FastAPI's detail. */
function errText(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  try {
    const j = JSON.parse(raw) as { detail?: unknown }
    if (j?.detail) return typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
  } catch {}
  return raw
}

/* ------------------------------------------------------------------ thread */

type SendError = { message: string; beforeFirstByte: boolean }

export function Thread({ id }: { id: string }) {
  const conv = useResource<ConversationDetail>(K.conversation(id), { tier: 'static' })
  const agents = useResource<Agent[]>(K.agents, { tier: 'lazy' })
  const run = useAction()

  const [input, setInput] = useState('')
  const [pending, setPending] = useState<string | null>(null) // in-flight user msg
  const [streaming, setStreaming] = useState(false)
  const [sendError, setSendError] = useState<SendError | null>(null)
  const [convSheet, setConvSheet] = useState(false)
  const [agentSheet, setAgentSheet] = useState(false)
  const [toasts, setToasts] = useState<Toast[]>([])

  const storeRef = useRef<StreamStore | null>(null)
  if (!storeRef.current) storeRef.current = createStreamStore()
  const store = storeRef.current

  const abortRef = useRef<AbortController | null>(null)
  const paneRef = useRef<HTMLDivElement>(null)
  const pinnedRef = useRef(true)
  const coarseRef = useRef(false)

  useEffect(() => {
    coarseRef.current = window.matchMedia('(pointer: coarse)').matches
  }, [])

  // Navigation/unmount must not leave the server generating into a dead socket.
  useEffect(() => () => abortRef.current?.abort(), [])

  const toast = (tone: Toast['tone'], text: string) => {
    const tid = Date.now() + Math.random()
    setToasts((t) => [...t, { id: tid, tone, text }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== tid)), 6000)
  }

  /** Pinned = the reader is at (or within 80px of) the bottom. Updated from
      real scroll events, so a programmatic scroll re-pins and a human
      scrolling up to re-read un-pins — that is the entire follow policy. */
  const onPaneScroll = useCallback(() => {
    const p = paneRef.current
    if (!p) return
    pinnedRef.current = p.scrollHeight - p.scrollTop - p.clientHeight < 80
  }, [])

  const scrollToEnd = useCallback(() => {
    const p = paneRef.current
    if (p) p.scrollTop = p.scrollHeight
  }, [])

  const followIfPinned = useCallback(() => {
    if (pinnedRef.current) scrollToEnd()
  }, [scrollToEnd])

  // Open at the latest message; afterwards follow only while pinned.
  const messages = conv.data?.messages
  const initRef = useRef(false)
  useLayoutEffect(() => {
    if (!messages) return
    if (!initRef.current) {
      initRef.current = true
      scrollToEnd()
    } else {
      followIfPinned()
    }
  }, [messages, scrollToEnd, followIfPinned])

  // A different conversation id = a different pane; re-run the initial scroll.
  useEffect(() => {
    initRef.current = false
    pinnedRef.current = true
  }, [id])

  async function send(contentArg?: string) {
    const content = (contentArg ?? input).trim()
    if (!content || streaming || !conv.data) return

    setInput('')
    setSendError(null)
    setPending(content)
    setStreaming(true)
    store.reset()
    pinnedRef.current = true
    // Wait a frame so the pending row exists before we scroll to it.
    requestAnimationFrame(scrollToEnd)

    const ac = new AbortController()
    abortRef.current = ac
    let gotFirstByte = false

    const attempt = async () => {
      for await (const ev of openSse(`/conversations/${id}/messages`, {
        method: 'POST',
        body: { content, stream: true },
        signal: ac.signal,
      })) {
        gotFirstByte = true
        let data: ChatStreamData
        try {
          data = JSON.parse(ev.data) as ChatStreamData
        } catch {
          continue
        }
        const kind = ev.event !== 'message' ? ev.event : data.type
        if (kind === 'text' && data.content) store.push(data.content)
        else if (kind === 'tool_call' && data.tool_call?.name) store.pushTool(data.tool_call.name)
        else if (kind === 'error') throw new Error(data.error || 'The model returned an error')
        else if (kind === 'done') return
      }
    }

    try {
      try {
        await attempt()
      } catch (err) {
        if (ac.signal.aborted) throw err
        // Retry once, ONLY for a network failure before any byte arrived —
        // the request may never have reached the server. An HTTP refusal or a
        // mid-stream error means the server saw the turn; re-POSTing it would
        // duplicate the turn and start a second generation.
        if (!gotFirstByte && err instanceof TypeError) {
          await attempt()
        } else {
          throw err
        }
      }
      // The turn is persisted server-side; refetch so the assistant reply
      // renders from the durable record (with markdown) before the local
      // streaming copies are cleared.
      await conv.refresh()
      void swrMutate((k) => typeof k === 'string' && k.startsWith('/conversations?'), undefined, { revalidate: true })
    } catch (err) {
      if (ac.signal.aborted) {
        // Human pressed Stop (or navigated): not an error. The server may have
        // persisted a partial turn — refetch to show whatever survived.
        toast('ok', 'Generation stopped')
        await conv.refresh()
      } else {
        const message = errText(err)
        setSendError({ message, beforeFirstByte: !gotFirstByte })
        if (!gotFirstByte) {
          // Nothing reached the model; hand the text back so Send IS the retry.
          setInput(content)
        } else {
          await conv.refresh()
        }
      }
    } finally {
      abortRef.current = null
      setStreaming(false)
      setPending(null)
      store.reset()
    }
  }

  async function switchAgent(agent: Agent) {
    if (!agent.slug) return
    const updated = await run(() => switchConversationMode(id, agent.slug!), {
      invalidate: ['/conversations?'],
      onError: (e) => toast('warn', `Switch failed: ${e.message}`),
    })
    if (updated) {
      await conv.mutate(updated as ConversationDetail, { revalidate: false })
      setAgentSheet(false)
      toast('ok', `Mode: ${agent.name}`)
    }
  }

  const activeAgent = agentById(agents.data, conv.data?.active_agent_id ?? conv.data?.agent_id)
  const AgentIcon = agentIcon(activeAgent)
  const title = conv.data?.title || 'Conversation'

  function onComposerKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Touch keyboards: Enter is newline (the visible Send button submits) —
    // iOS has no Shift key worth the name. Hardware keyboards: Enter sends.
    if (e.key === 'Enter' && !e.shiftKey && !coarseRef.current) {
      e.preventDefault()
      void send()
    }
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {/* ONE header row (the old page stacked three bands totalling 277px of an
          844px screen). Title opens the conversation switcher; the agent chip
          opens the mode switcher. */}
      <div className="flex min-h-control shrink-0 items-center gap-1 border-b border-line px-safe py-1">
        <button
          onClick={() => setConvSheet(true)}
          aria-haspopup="dialog"
          className="flex min-h-control min-w-0 flex-1 items-center gap-1.5 rounded-sm px-1 text-left hover:bg-panel-2"
        >
          <span className="min-w-0 truncate font-sans text-body text-ink">{title}</span>
          <ChevronDown size={14} aria-hidden="true" className="shrink-0 text-ink-faint" />
        </button>
        <button
          onClick={() => setAgentSheet(true)}
          disabled={streaming}
          aria-haspopup="dialog"
          className="flex min-h-control shrink-0 items-center gap-1.5 rounded-sm border border-line px-2.5 text-micro uppercase tracking-[0.06em] text-ink-dim hover:border-ink-faint hover:text-ink disabled:opacity-40"
        >
          <AgentIcon size={14} aria-hidden="true" />
          <span className="max-w-32 truncate normal-case tracking-normal">{activeAgent?.name ?? 'Agent'}</span>
        </button>
      </div>

      {/* The scroller. THIS pane moves during streaming, never the document. */}
      <div ref={paneRef} onScroll={onPaneScroll} className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain px-safe py-3">
        <div className="mx-auto flex w-full max-w-3xl min-w-0 flex-col gap-4">
          <Async r={conv} skeletonRows={6}>
            {(data) => (
              <>
                {data.messages.length === 0 && !pending && (
                  <EmptyState>No messages yet — say something below.</EmptyState>
                )}
                {data.messages.map((m, i) => (
                  <MessageRow key={m.id ?? i} msg={m} />
                ))}
                {pending !== null && <MessageRow msg={{ role: 'user', content: pending }} />}
                {streaming && <StreamingRow store={store} onGrow={followIfPinned} />}
              </>
            )}
          </Async>
        </div>
      </div>

      {sendError && (
        <div className="shrink-0 px-safe pb-1">
          <Notice tone="warn" className="mx-auto max-w-3xl">
            <span className="wrap-anywhere">
              {sendError.message}
              {sendError.beforeFirstByte
                ? ' — your message was not delivered; it is back in the composer.'
                : ' — the turn may be partially recorded above. Not re-sent automatically.'}
            </span>
          </Notice>
        </div>
      )}

      {/* Composer. Below lg it must clear the FIXED bottom tab bar (the shell
          pads non-flush mains for this; a flush page owns it) plus the home
          indicator; the shell's --vvh height keeps it above the iOS keyboard. */}
      <div className="shrink-0 border-t border-line bg-panel px-safe py-2 pb-[calc(var(--tabbar-h)+var(--sab)+0.5rem)] lg:pb-2">
        <form
          className="mx-auto flex w-full max-w-3xl items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            void send()
          }}
        >
          <Textarea
            value={input}
            onChange={(e) => {
              setInput(e.target.value)
              // Auto-grow, capped by the max-h class; height:auto first so
              // deleting lines shrinks it back.
              const el = e.currentTarget
              el.style.height = 'auto'
              el.style.height = `${el.scrollHeight}px`
            }}
            onKeyDown={onComposerKeyDown}
            placeholder="Message"
            aria-label="Message"
            rows={1}
            className="max-h-40 resize-none overflow-y-auto"
            disabled={conv.data === undefined}
          />
          {streaming ? (
            <Button type="button" variant="danger" onClick={() => abortRef.current?.abort()}>
              <Square size={13} aria-hidden="true" />
              Stop
            </Button>
          ) : (
            <Button type="submit" variant="primary" disabled={!input.trim() || conv.data === undefined}>
              <Send size={14} aria-hidden="true" />
              Send
            </Button>
          )}
        </form>
      </div>

      <Sheet open={convSheet} onClose={() => setConvSheet(false)} title="Conversations">
        <ConversationList frame="sheet" onNavigate={() => setConvSheet(false)} />
      </Sheet>

      <Sheet open={agentSheet} onClose={() => setAgentSheet(false)} title="Switch mode">
        {/* Async, not a bare read — see ConversationList: an unloaded agents
            resource must render as loading/error, not "No enabled agents". */}
        <Async r={agents} skeletonRows={3}>
          {(rows) => (
            <AgentPicker
              agents={enabledAgents(rows)}
              currentId={activeAgent?.id}
              onPick={(a) => void switchAgent(a)}
              hint="Changes which agent answers from the next message on."
            />
          )}
        </Async>
      </Sheet>

      <Toasts toasts={toasts} onDismiss={(tid) => setToasts((t) => t.filter((x) => x.id !== tid))} />
    </div>
  )
}
