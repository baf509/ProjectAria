'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { shellsApi, type Shell, type ShellEvent } from '@/lib/api-client-shells'

type ViewMode = 'snapshot' | 'stream'

const SPECIAL_KEYS: Array<{ label: string; text: string; literal?: boolean; appendEnter?: boolean }> = [
  { label: 'Enter', text: 'Enter', appendEnter: false },
  { label: 'Esc', text: 'Escape', appendEnter: false },
  { label: '⌃C', text: 'C-c', appendEnter: false },
  { label: '⌃D', text: 'C-d', appendEnter: false },
  { label: '↑', text: 'Up', appendEnter: false },
  { label: '↓', text: 'Down', appendEnter: false },
  { label: 'yes', text: 'yes' },
  { label: 'no', text: 'no' },
]

const NOISE_PATTERNS = [
  /^Checking for updates$/i,
  /^Auto-update.*$/i,
]

function relativeTime(iso: string): string {
  const delta = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (delta < 5) return 'just now'
  if (delta < 60) return `${delta}s ago`
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`
  return `${Math.floor(delta / 86400)}d ago`
}

function statusDot(status: Shell['status']): string {
  switch (status) {
    case 'active':
      return 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.6)]'
    case 'idle':
      return 'bg-amber-400'
    case 'stopped':
      return 'bg-stone-600'
    default:
      return 'bg-stone-500'
  }
}

function statusBadge(status: Shell['status']): string {
  switch (status) {
    case 'active':
      return 'text-emerald-300 bg-emerald-950/60 border-emerald-900'
    case 'idle':
      return 'text-amber-300 bg-amber-950/60 border-amber-900'
    case 'stopped':
      return 'text-stone-400 bg-stone-900 border-stone-800'
    default:
      return 'text-stone-400 bg-stone-900 border-stone-800'
  }
}

interface DisplayLine {
  key: string
  kind: ShellEvent['kind']
  text: string
  count: number
  ts: string
  lastLine: number
}

function isNoise(text: string): boolean {
  if (!text.trim()) return true
  return NOISE_PATTERNS.some((re) => re.test(text.trim()))
}

function buildDisplayLines(events: ShellEvent[], hideNoise: boolean): DisplayLine[] {
  const out: DisplayLine[] = []
  for (const e of events) {
    const text = e.text_clean
    if (hideNoise && isNoise(text)) continue
    const trimmed = text.replace(/\s+$/, '')
    const last = out[out.length - 1]
    if (last && last.kind === e.kind && last.text === trimmed) {
      last.count += 1
      last.ts = e.ts
      last.lastLine = e.line_number
      continue
    }
    out.push({
      key: `${e.shell_name}:${e.line_number}`,
      kind: e.kind,
      text: trimmed,
      count: 1,
      ts: e.ts,
      lastLine: e.line_number,
    })
  }
  return out
}

export default function ShellsDashboardPage() {
  const [shells, setShells] = useState<Shell[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [events, setEvents] = useState<ShellEvent[]>([])
  const [snapshot, setSnapshot] = useState<{ content: string; ts: string } | null>(null)
  const [viewMode, setViewMode] = useState<ViewMode>('snapshot')
  const [hideNoise, setHideNoise] = useState(true)
  const [filterText, setFilterText] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | Shell['status']>('all')
  const [inputText, setInputText] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showJumpToBottom, setShowJumpToBottom] = useState(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const sseRef = useRef<EventSource | null>(null)
  const atBottomRef = useRef<boolean>(true)
  const inputRef = useRef<HTMLInputElement | null>(null)

  const refreshShells = useCallback(async () => {
    try {
      const list = await shellsApi.list()
      list.sort((a, b) => new Date(b.last_activity_at).getTime() - new Date(a.last_activity_at).getTime())
      setShells(list)
      setSelected((prev) => prev ?? list[0]?.name ?? null)
    } catch (e: any) {
      setError(e?.message || 'Failed to list shells')
    }
  }, [])

  useEffect(() => {
    refreshShells()
    const id = setInterval(refreshShells, 10_000)
    return () => clearInterval(id)
  }, [refreshShells])

  // Stream events for selected shell.
  useEffect(() => {
    if (!selected) return
    let cancelled = false
    setEvents([])
    ;(async () => {
      try {
        const initial = await shellsApi.listEvents(selected, { limit: 500 })
        if (!cancelled) setEvents(initial)
      } catch (e: any) {
        if (!cancelled) setError(e?.message || 'Failed to load events')
      }
    })()

    const url = shellsApi.streamUrl(selected)
    const src = new EventSource(url)
    sseRef.current = src
    src.addEventListener('shell_event', (ev: MessageEvent) => {
      try {
        const evt: ShellEvent = JSON.parse(ev.data)
        setEvents((prev) => {
          if (prev.length && prev[prev.length - 1].line_number >= evt.line_number) return prev
          return [...prev, evt].slice(-1500)
        })
      } catch {}
    })
    src.addEventListener('shell_status', () => refreshShells())
    src.onerror = () => {
      // EventSource will auto-reconnect.
    }
    return () => {
      cancelled = true
      src.close()
      sseRef.current = null
    }
  }, [selected, refreshShells])

  // Poll snapshot when in snapshot mode.
  useEffect(() => {
    if (!selected || viewMode !== 'snapshot') return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const fetchSnap = async () => {
      try {
        const snap = await shellsApi.getSnapshot(selected)
        if (cancelled) return
        if (snap) setSnapshot({ content: snap.content, ts: snap.ts })
        else setSnapshot(null)
      } catch (e: any) {
        if (!cancelled) setError(e?.message || 'Failed to load snapshot')
      } finally {
        if (!cancelled) timer = setTimeout(fetchSnap, 3_000)
      }
    }
    fetchSnap()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [selected, viewMode])

  const displayLines = useMemo(() => buildDisplayLines(events, hideNoise), [events, hideNoise])

  // Auto-scroll behavior for stream view.
  useEffect(() => {
    if (viewMode !== 'stream') return
    const el = scrollRef.current
    if (!el) return
    if (atBottomRef.current) el.scrollTop = el.scrollHeight
  }, [displayLines, viewMode])

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight
    const near = distance < 40
    atBottomRef.current = near
    setShowJumpToBottom(!near && distance > 200)
  }

  function jumpToBottom() {
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    atBottomRef.current = true
    setShowJumpToBottom(false)
  }

  async function handleSend(text: string, opts: { appendEnter?: boolean; literal?: boolean } = {}) {
    if (!selected || !text) return
    setSending(true)
    try {
      await shellsApi.sendInput(selected, text, opts)
      if (opts.appendEnter !== false) setInputText('')
      inputRef.current?.focus()
    } catch (e: any) {
      setError(e?.message || 'Failed to send input')
    } finally {
      setSending(false)
    }
  }

  const filteredShells = useMemo(() => {
    const q = filterText.trim().toLowerCase()
    return shells.filter((s) => {
      if (statusFilter !== 'all' && s.status !== statusFilter) return false
      if (!q) return true
      return (
        s.short_name.toLowerCase().includes(q) ||
        s.name.toLowerCase().includes(q) ||
        s.project_dir.toLowerCase().includes(q) ||
        s.tags.some((t) => t.toLowerCase().includes(q))
      )
    })
  }, [shells, filterText, statusFilter])

  const counts = useMemo(() => {
    const c = { all: shells.length, active: 0, idle: 0, stopped: 0, unknown: 0 }
    for (const s of shells) c[s.status as keyof typeof c] = (c[s.status as keyof typeof c] || 0) + 1
    return c
  }, [shells])

  const currentShell = shells.find((s) => s.name === selected) || null

  return (
    <div className="min-h-screen bg-stone-950 text-stone-100">
      <header className="border-b border-stone-800 bg-stone-950/80 backdrop-blur px-6 py-4">
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-[10px] uppercase tracking-[0.3em] text-fuchsia-400">Watched Shells</p>
            <h1 className="text-2xl font-semibold text-stone-50">Terminal Sessions</h1>
          </div>
          <div className="flex items-center gap-4 text-sm">
            <span className="text-stone-400">
              <span className="text-stone-200 font-semibold">{counts.active}</span> active ·{' '}
              <span className="text-stone-200 font-semibold">{counts.idle}</span> idle ·{' '}
              <span className="text-stone-200 font-semibold">{counts.stopped}</span> stopped
            </span>
            <a href="/dashboard" className="text-stone-400 hover:text-stone-200">
              ← Dashboard
            </a>
          </div>
        </div>
      </header>

      {error && (
        <div className="bg-red-950/60 border-b border-red-900 px-6 py-2 text-sm text-red-200" role="alert">
          {error}
          <button className="ml-4 underline hover:text-red-100" onClick={() => setError(null)}>
            dismiss
          </button>
        </div>
      )}

      <div className="flex flex-col md:flex-row h-[calc(100vh-72px)]">
        {/* Sidebar: shell list */}
        <aside className="md:w-80 border-r border-stone-800 flex flex-col">
          <div className="p-3 border-b border-stone-800 space-y-2">
            <input
              type="text"
              placeholder="Filter by name, project, tag…"
              value={filterText}
              onChange={(e) => setFilterText(e.target.value)}
              className="w-full bg-stone-900 border border-stone-800 rounded-md px-3 py-1.5 text-sm placeholder:text-stone-600 focus:outline-none focus:border-fuchsia-500"
            />
            <div className="flex gap-1 flex-wrap">
              {(['all', 'active', 'idle', 'stopped'] as const).map((s) => {
                const active = statusFilter === s
                const count = (counts as Record<string, number>)[s] ?? 0
                return (
                  <button
                    key={s}
                    onClick={() => setStatusFilter(s)}
                    className={`px-2 py-0.5 text-xs rounded-full border transition ${
                      active
                        ? 'bg-fuchsia-500/20 text-fuchsia-200 border-fuchsia-700'
                        : 'bg-stone-900 text-stone-400 border-stone-800 hover:border-stone-700'
                    }`}
                  >
                    {s} <span className="opacity-70">{count}</span>
                  </button>
                )
              })}
            </div>
          </div>

          <div className="flex-1 overflow-y-auto">
            {filteredShells.length === 0 && (
              <div className="p-6 text-sm text-stone-500">
                {shells.length === 0 ? (
                  <>
                    <p className="mb-2 text-stone-300 font-medium">No watched shells yet</p>
                    <p>Start one with:</p>
                    <code className="block mt-2 px-2 py-1 bg-stone-900 rounded text-xs text-fuchsia-300">
                      aria shells new
                    </code>
                    <p className="mt-2">or</p>
                    <code className="block mt-2 px-2 py-1 bg-stone-900 rounded text-xs text-fuchsia-300">
                      ac projectaria
                    </code>
                  </>
                ) : (
                  <>No shells match the current filter.</>
                )}
              </div>
            )}
            {filteredShells.map((s) => {
              const active = s.name === selected
              return (
                <button
                  key={s.name}
                  onClick={() => setSelected(s.name)}
                  className={`w-full text-left px-4 py-3 border-b border-stone-900 transition relative ${
                    active
                      ? 'bg-stone-900 border-l-2 border-l-fuchsia-500'
                      : 'hover:bg-stone-900/60 border-l-2 border-l-transparent'
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <span className={`w-2 h-2 rounded-full ${statusDot(s.status)}`} />
                    <span className="font-mono font-semibold text-sm text-stone-100 truncate">
                      {s.short_name}
                    </span>
                    <span className="ml-auto text-[10px] text-stone-500 shrink-0">
                      {relativeTime(s.last_activity_at)}
                    </span>
                  </div>
                  <div className="text-xs text-stone-400 mt-1 truncate" title={s.project_dir || s.name}>
                    {s.project_dir || s.name}
                  </div>
                  <div className="flex items-center gap-2 mt-1.5">
                    <span
                      className={`px-1.5 py-0.5 text-[9px] uppercase tracking-wider rounded border ${statusBadge(
                        s.status
                      )}`}
                    >
                      {s.status}
                    </span>
                    <span className="text-[10px] text-stone-500">{s.line_count.toLocaleString()} lines</span>
                    {s.tags.length > 0 && (
                      <span className="text-[10px] text-stone-500 truncate">· {s.tags.join(', ')}</span>
                    )}
                  </div>
                </button>
              )
            })}
          </div>
        </aside>

        {/* Detail */}
        <section className="flex-1 flex flex-col min-w-0">
          {currentShell ? (
            <>
              {/* Detail header */}
              <div className="px-4 py-3 border-b border-stone-800 flex items-center gap-3 text-sm flex-wrap">
                <span className={`w-2.5 h-2.5 rounded-full ${statusDot(currentShell.status)}`} />
                <div className="flex flex-col">
                  <span className="font-mono font-semibold text-stone-100">{currentShell.short_name}</span>
                  <span className="text-[10px] text-stone-500 truncate" title={currentShell.project_dir}>
                    {currentShell.project_dir || currentShell.name}
                  </span>
                </div>
                <span
                  className={`px-2 py-0.5 text-[10px] uppercase tracking-wider rounded border ${statusBadge(
                    currentShell.status
                  )}`}
                >
                  {currentShell.status}
                </span>
                <span className="text-stone-500 text-xs ml-auto">
                  last activity {relativeTime(currentShell.last_activity_at)}
                </span>

                {/* View mode toggle */}
                <div className="inline-flex rounded-md border border-stone-800 bg-stone-900 overflow-hidden">
                  {(['snapshot', 'stream'] as const).map((m) => (
                    <button
                      key={m}
                      onClick={() => setViewMode(m)}
                      className={`px-3 py-1 text-xs uppercase tracking-wider ${
                        viewMode === m ? 'bg-fuchsia-500/20 text-fuchsia-200' : 'text-stone-400 hover:text-stone-200'
                      }`}
                    >
                      {m}
                    </button>
                  ))}
                </div>
                {viewMode === 'stream' && (
                  <label className="flex items-center gap-1.5 text-xs text-stone-400 cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={hideNoise}
                      onChange={(e) => setHideNoise(e.target.checked)}
                      className="accent-fuchsia-500"
                    />
                    Hide noise
                  </label>
                )}
              </div>

              {/* Body */}
              <div className="flex-1 relative overflow-hidden">
                {viewMode === 'snapshot' ? (
                  <div className="absolute inset-0 overflow-y-auto bg-black font-mono text-xs px-4 py-3">
                    {snapshot ? (
                      <>
                        <div className="text-stone-500 text-[10px] mb-2 uppercase tracking-widest">
                          Rendered terminal · {relativeTime(snapshot.ts)}
                        </div>
                        <pre className="whitespace-pre text-stone-200 leading-snug">{snapshot.content}</pre>
                      </>
                    ) : (
                      <div className="text-stone-500 italic">
                        No snapshot available yet for this shell. Switch to Stream to see the raw scrollback.
                      </div>
                    )}
                  </div>
                ) : (
                  <>
                    <div
                      ref={scrollRef}
                      onScroll={onScroll}
                      className="absolute inset-0 overflow-y-auto bg-black font-mono text-xs px-4 py-2 whitespace-pre-wrap"
                    >
                      {displayLines.length === 0 && (
                        <div className="text-stone-600 italic">
                          {hideNoise
                            ? 'No content yet (noise filter is on — try toggling it off if expected output is missing).'
                            : 'No events yet for this shell.'}
                        </div>
                      )}
                      {displayLines.map((l) => (
                        <div
                          key={l.key}
                          className={`flex items-start gap-2 ${
                            l.kind === 'input'
                              ? 'text-emerald-300'
                              : l.kind === 'system'
                              ? 'text-amber-300'
                              : 'text-stone-200'
                          }`}
                          title={new Date(l.ts).toLocaleString()}
                        >
                          <span className="flex-1 min-w-0">
                            {l.kind === 'input' ? '> ' : ''}
                            {l.text || ' '}
                          </span>
                          {l.count > 1 && (
                            <span className="text-[10px] text-stone-500 bg-stone-900 border border-stone-800 rounded px-1.5 py-0 shrink-0 mt-0.5">
                              ×{l.count}
                            </span>
                          )}
                        </div>
                      ))}
                    </div>
                    {showJumpToBottom && (
                      <button
                        onClick={jumpToBottom}
                        className="absolute bottom-3 right-4 px-3 py-1.5 text-xs rounded-full bg-fuchsia-500 text-stone-950 font-semibold shadow-lg hover:bg-fuchsia-400"
                      >
                        ↓ Jump to bottom
                      </button>
                    )}
                  </>
                )}
              </div>

              {/* Input */}
              <div className="border-t border-stone-800 p-3 space-y-2 bg-stone-950">
                <form
                  onSubmit={(e) => {
                    e.preventDefault()
                    handleSend(inputText)
                  }}
                  className="flex gap-2"
                >
                  <input
                    ref={inputRef}
                    className="flex-1 bg-stone-900 border border-stone-800 rounded-md px-3 py-2 font-mono text-sm placeholder:text-stone-600 focus:outline-none focus:border-fuchsia-500 disabled:opacity-50"
                    placeholder={
                      currentShell.status === 'stopped'
                        ? 'Shell is stopped — input disabled'
                        : 'Type and press Enter to send into the shell'
                    }
                    value={inputText}
                    onChange={(e) => setInputText(e.target.value)}
                    disabled={sending || currentShell.status === 'stopped'}
                    autoComplete="off"
                  />
                  <button
                    type="submit"
                    disabled={sending || !inputText || currentShell.status === 'stopped'}
                    className="px-4 py-2 bg-fuchsia-500 hover:bg-fuchsia-400 rounded-md text-sm font-semibold text-stone-950 disabled:opacity-40"
                  >
                    Send
                  </button>
                </form>
                <div className="flex gap-1.5 flex-wrap">
                  {SPECIAL_KEYS.map((k) => (
                    <button
                      key={k.label}
                      onClick={() => handleSend(k.text, { literal: !!k.literal, appendEnter: !!k.appendEnter })}
                      disabled={sending || currentShell.status === 'stopped'}
                      className="px-2 py-1 text-[11px] font-mono bg-stone-900 hover:bg-stone-800 rounded border border-stone-800 hover:border-stone-700 disabled:opacity-40"
                    >
                      {k.label}
                    </button>
                  ))}
                </div>
              </div>
            </>
          ) : (
            <div className="flex-1 flex items-center justify-center text-stone-500">
              {shells.length === 0
                ? 'Start a watched shell with `aria shells new` to get started.'
                : 'Select a shell to view its scrollback.'}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
