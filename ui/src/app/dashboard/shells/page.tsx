'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { shellsApi, type Shell, type ShellEvent } from '@/lib/api-client-shells'

const SPECIAL_KEYS: Array<{ label: string; text: string; literal?: boolean; appendEnter?: boolean }> = [
  { label: 'Enter', text: 'Enter', appendEnter: false },
  { label: 'Esc', text: 'Escape', appendEnter: false },
  { label: 'Ctrl-C', text: 'C-c', appendEnter: false },
  { label: 'Ctrl-D', text: 'C-d', appendEnter: false },
  { label: '↑', text: 'Up', appendEnter: false },
  { label: '↓', text: 'Down', appendEnter: false },
  { label: 'Yes', text: 'yes' },
  { label: 'No', text: 'no' },
]

function relativeTime(iso: string): string {
  const delta = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (delta < 60) return `${delta}s ago`
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`
  return `${Math.floor(delta / 86400)}d ago`
}

function statusColor(status: Shell['status']): string {
  switch (status) {
    case 'active':
      return 'bg-emerald-500'
    case 'idle':
      return 'bg-amber-500'
    case 'stopped':
      return 'bg-zinc-500'
    default:
      return 'bg-zinc-400'
  }
}

export default function ShellsDashboardPage() {
  const [shells, setShells] = useState<Shell[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [events, setEvents] = useState<ShellEvent[]>([])
  const [inputText, setInputText] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const sseRef = useRef<EventSource | null>(null)
  const atBottomRef = useRef<boolean>(true)

  const refreshShells = useCallback(async () => {
    try {
      const list = await shellsApi.list()
      setShells(list)
      if (!selected && list.length) {
        setSelected(list[0].name)
      }
    } catch (e: any) {
      setError(e?.message || 'Failed to list shells')
    }
  }, [selected])

  useEffect(() => {
    refreshShells()
    const id = setInterval(refreshShells, 10_000)
    return () => clearInterval(id)
  }, [refreshShells])

  // Initial scrollback fetch + SSE tail on shell selection.
  useEffect(() => {
    if (!selected) return
    let cancelled = false
    setEvents([])
    ;(async () => {
      try {
        const initial = await shellsApi.listEvents(selected, { limit: 500 })
        if (cancelled) return
        setEvents(initial)
      } catch (e: any) {
        setError(e?.message || 'Failed to load events')
      }
    })()

    // Open SSE
    const url = shellsApi.streamUrl(selected)
    const src = new EventSource(url)
    sseRef.current = src
    src.addEventListener('shell_event', (ev: MessageEvent) => {
      try {
        const evt: ShellEvent = JSON.parse(ev.data)
        setEvents((prev) => {
          if (prev.length && prev[prev.length - 1].line_number >= evt.line_number) {
            return prev
          }
          return [...prev, evt].slice(-1000)
        })
      } catch {}
    })
    src.addEventListener('shell_status', () => refreshShells())
    src.onerror = () => {
      // Let EventSource auto-reconnect.
    }
    return () => {
      cancelled = true
      src.close()
      sseRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected])

  // Auto-scroll behavior
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    if (atBottomRef.current) {
      el.scrollTop = el.scrollHeight
    }
  }, [events])

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    atBottomRef.current = near
  }

  async function handleSend(text: string, opts: { appendEnter?: boolean; literal?: boolean } = {}) {
    if (!selected || !text) return
    setSending(true)
    try {
      await shellsApi.sendInput(selected, text, opts)
      if (opts.appendEnter !== false) setInputText('')
    } catch (e: any) {
      setError(e?.message || 'Failed to send input')
    } finally {
      setSending(false)
    }
  }

  async function handleSpecial(text: string, literal = false, appendEnter = false) {
    await handleSend(text, { literal, appendEnter })
  }

  const currentShell = shells.find((s) => s.name === selected) || null

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <header className="border-b border-zinc-800 px-6 py-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold">Watched Shells</h1>
        <a href="/dashboard" className="text-sm text-zinc-400 hover:text-zinc-200">
          ← Dashboard
        </a>
      </header>

      {error && (
        <div className="bg-red-900/40 border border-red-800 px-4 py-2 text-sm" role="alert">
          {error}
          <button className="ml-4 underline" onClick={() => setError(null)}>
            dismiss
          </button>
        </div>
      )}

      <div className="flex flex-col md:flex-row h-[calc(100vh-64px)]">
        {/* List */}
        <aside className="md:w-72 border-r border-zinc-800 overflow-y-auto">
          {shells.length === 0 && (
            <div className="p-4 text-sm text-zinc-500">No shells yet. Start one with <code>ac projectaria</code>.</div>
          )}
          {shells.map((s) => {
            const active = s.name === selected
            return (
              <button
                key={s.name}
                onClick={() => setSelected(s.name)}
                className={`w-full text-left px-4 py-3 border-b border-zinc-800 hover:bg-zinc-900 ${
                  active ? 'bg-zinc-900' : ''
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full ${statusColor(s.status)}`} />
                  <span className="font-mono font-semibold">{s.short_name}</span>
                  <span className="ml-auto text-xs text-zinc-500">{relativeTime(s.last_activity_at)}</span>
                </div>
                <div className="text-xs text-zinc-500 mt-1 truncate">{s.project_dir || s.name}</div>
                <div className="text-xs text-zinc-600 mt-1">
                  {s.line_count} lines · {s.status}
                  {s.tags.length ? ` · ${s.tags.join(', ')}` : ''}
                </div>
              </button>
            )
          })}
        </aside>

        {/* Detail */}
        <section className="flex-1 flex flex-col min-w-0">
          {currentShell ? (
            <>
              <div className="px-4 py-3 border-b border-zinc-800 flex items-center gap-3 text-sm">
                <span className={`w-2 h-2 rounded-full ${statusColor(currentShell.status)}`} />
                <span className="font-mono font-semibold">{currentShell.short_name}</span>
                <span className="text-zinc-500">{currentShell.status}</span>
                <span className="text-zinc-500 truncate">{currentShell.project_dir}</span>
                <span className="ml-auto text-zinc-500">
                  last activity {relativeTime(currentShell.last_activity_at)}
                </span>
              </div>

              <div
                ref={scrollRef}
                onScroll={onScroll}
                className="flex-1 overflow-y-auto bg-black font-mono text-xs px-4 py-2 whitespace-pre-wrap"
              >
                {events.map((e) => (
                  <div
                    key={`${e.shell_name}:${e.line_number}`}
                    className={e.kind === 'input' ? 'text-emerald-400' : e.kind === 'system' ? 'text-amber-400' : 'text-zinc-200'}
                  >
                    {e.kind === 'input' ? '> ' : ''}
                    {e.text_clean}
                  </div>
                ))}
              </div>

              <div className="border-t border-zinc-800 p-3 space-y-2">
                <div className="flex gap-2 flex-wrap">
                  {SPECIAL_KEYS.map((k) => (
                    <button
                      key={k.label}
                      onClick={() => handleSpecial(k.text, false, !!k.appendEnter)}
                      disabled={sending}
                      className="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded border border-zinc-700"
                    >
                      {k.label}
                    </button>
                  ))}
                </div>
                <form
                  onSubmit={(e) => {
                    e.preventDefault()
                    handleSend(inputText)
                  }}
                  className="flex gap-2"
                >
                  <input
                    className="flex-1 bg-zinc-900 border border-zinc-700 rounded px-3 py-2 font-mono text-sm"
                    placeholder="Type and press Enter to send"
                    value={inputText}
                    onChange={(e) => setInputText(e.target.value)}
                    disabled={sending || currentShell.status === 'stopped'}
                    autoComplete="off"
                  />
                  <button
                    type="submit"
                    disabled={sending || !inputText || currentShell.status === 'stopped'}
                    className="px-4 py-2 bg-emerald-700 hover:bg-emerald-600 rounded text-sm font-semibold disabled:opacity-50"
                  >
                    Send
                  </button>
                </form>
              </div>
            </>
          ) : (
            <div className="flex-1 flex items-center justify-center text-zinc-500">
              Select a shell to view its scrollback.
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
