'use client'

/**
 * ARIA - shell terminal (detail pane)
 *
 * The measured defects this file exists to fix, in the order they hurt:
 *  - The old SSE stream opened with NO `since_line`, so the server replayed
 *    from line 0 — for claude-ProjectAria (7M lines) that was ~98 minutes of
 *    replay before "live". Every open here carries
 *    `since_line = max(0, line_count - 300)` and every REopen resumes from the
 *    buffer's high-water mark at a NEW url (openSse, not EventSource — an
 *    EventSource reconnects to its fixed url, replaying the backlog again
 *    after every iOS background/foreground cycle, and needed the master API
 *    key in its query string).
 *  - Every SSE line called setState on a 780-line component. The stream now
 *    writes into TermBuffer (rAF-coalesced, outside React) and rows are
 *    memoised, so a 500-line catch-up burst is one render.
 *  - The default pane is /screen (~1KB) polled at tier 'live' ONLY while this
 *    pane is mounted and visible; the pane component is memoised on the screen
 *    string, so an unchanged poll re-renders nothing (the old page replaced a
 *    75KB <pre> every 3s regardless).
 *  - The terminal was 209 unwrapped columns in a two-axis scroller nested in a
 *    scrolling page. Default is WRAPPED (persisted toggle) with a font
 *    stepper; unwrapped lives inside ScrollX. Height comes from the flush
 *    shell's flex chain — no calc(100vh-…), no absolute inset-0 panes.
 */
import {
  FormEvent,
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react'
import { useResource, useAction } from '@/lib/swr'
import { K, resizeShell, sendShellKeys } from '@/lib/api/endpoints'
import type {
  ShellEventWire,
  ShellInfo,
  ShellScreenPayload,
  ShellSnapshotPayload,
} from '@/lib/api/types'
import { openSse } from '@/lib/stream'
import { Chip, Notice, Skeleton } from '@/components/ui/primitives'
import { Button, ConfirmButton, Input, Toasts } from '@/components/ui/controls'
import { Cluster, ScrollX } from '@/components/layout'
import { relativeTime } from '@/lib/time'
import { TermBuffer, type TermLine } from './termBuffer'
import { useToasts } from './useToasts'

/* ------------------------------------------------------------ persistence */

/**
 * localStorage-backed string pref. Read in an effect so the first server and
 * client renders agree (hydration), then corrected to the stored value.
 */
function usePersisted(key: string, fallback: string): [string, (v: string) => void] {
  const [value, setValue] = useState(fallback)
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(key)
      if (stored !== null) setValue(stored)
    } catch {
      /* private mode — session-only */
    }
  }, [key])
  const set = (v: string) => {
    setValue(v)
    try {
      window.localStorage.setItem(key, v)
    } catch {
      /* ignore */
    }
  }
  return [value, set]
}

/* -------------------------------------------------------------- constants */

// append_enter=false sends the tmux KEY NAME (Enter, Escape, C-c…); the two
// word buttons are literal text + Enter, for the yes/no prompts agents ask.
const SPECIAL_KEYS: Array<{ label: string; text: string; appendEnter: boolean }> = [
  { label: 'Enter', text: 'Enter', appendEnter: false },
  { label: 'Esc', text: 'Escape', appendEnter: false },
  { label: 'Tab', text: 'Tab', appendEnter: false },
  { label: '⌃C', text: 'C-c', appendEnter: false },
  { label: '⌃D', text: 'C-d', appendEnter: false },
  { label: '↑', text: 'Up', appendEnter: false },
  { label: '↓', text: 'Down', appendEnter: false },
  { label: 'yes', text: 'yes', appendEnter: true },
  { label: 'no', text: 'no', appendEnter: true },
]

const NOISE = [/^Checking for updates$/i, /^Auto-update.*$/i]
const isNoise = (text: string) => !text.trim() || NOISE.some((re) => re.test(text.trim()))

const FONT_MIN = 10
const FONT_MAX = 16

/* ------------------------------------------------------------- components */

/**
 * Memoised on the screen STRING: the 'live'-tier poll returns an identical
 * payload most ticks, and SWR's stable-hash compare keeps the same data
 * object, so this skips entirely when nothing changed on the pane.
 */
const ScreenPane = memo(function ScreenPane({ text, wrapped }: { text: string; wrapped: boolean }) {
  if (wrapped) {
    // Globals give bare <pre> pre-wrap; wrap-anywhere lets 209-column TUI
    // lines break instead of defining the page width.
    return <pre className="m-0 wrap-anywhere">{text}</pre>
  }
  return (
    <ScrollX>
      <pre className="pre m-0 w-max">{text}</pre>
    </ScrollX>
  )
})

const KIND_COLOR: Record<string, string> = {
  input: 'text-live',
  system: 'text-accent',
}

/**
 * One coalesced scrollback line. Memoised: the buffer replaces only the
 * objects that changed, so a 1500-line pane re-renders a handful of rows per
 * flush. The timestamp is formatted HERE, inside the memo boundary — the old
 * page called `toLocaleString()` for up to 1500 rows on every render.
 */
const LineRow = memo(function LineRow({
  line,
  showTs,
  wrapped,
}: {
  line: TermLine
  showTs: boolean
  wrapped: boolean
}) {
  return (
    <div className={`${wrapped ? 'whitespace-pre-wrap wrap-anywhere' : 'whitespace-pre'} ${KIND_COLOR[line.kind] ?? 'text-ink'}`}>
      {showTs && (
        <span className="tnum mr-2 text-ink-faint">
          {new Date(line.ts).toLocaleTimeString(undefined, { hour12: false })}
        </span>
      )}
      {line.kind === 'input' ? '> ' : ''}
      {line.text || ' '}
      {line.count > 1 && <span className="ml-2 rounded-sm border border-line bg-panel px-1 text-micro text-ink-faint">×{line.count}</span>}
    </div>
  )
})

/* ------------------------------------------------------------------ view */

type Mode = 'screen' | 'follow'
type Conn = 'idle' | 'open' | 'reconnecting'

export function TerminalView({ name }: { name: string }) {
  const { toasts, push, dismiss } = useToasts()
  const run = useAction()

  const shell = useResource<ShellInfo>(K.shell(name), { tier: 'fast' })
  const status = shell.data?.status
  const isStopped = status === 'stopped'
  const lineCount = shell.data?.line_count

  const [mode, setMode] = useState<Mode>('screen')
  const [wrapPref, setWrapPref] = usePersisted('aria.term.wrap', '1')
  const [fontPref, setFontPref] = usePersisted('aria.term.fs', '13')
  const [showTs, setShowTs] = useState(false)
  const [hideNoise, setHideNoise] = useState(true)
  const wrapped = wrapPref !== '0'
  const fontSize = Math.min(FONT_MAX, Math.max(FONT_MIN, Number(fontPref) || 13))

  // Screen mode: live pane while the shell runs; the stored snapshot is the
  // only pane view that survives a stop (`/screen` 409s without tmux).
  const screen = useResource<ShellScreenPayload>(K.shellScreen(name), {
    tier: 'live',
    enabled: mode === 'screen' && status !== undefined && !isStopped,
  })
  const snapshot = useResource<ShellSnapshotPayload>(K.shellSnapshot(name), {
    tier: 'slow',
    enabled: mode === 'screen' && isStopped,
  })

  /* ------------------------------------------------------------- follow */

  const bufRef = useRef<TermBuffer | null>(null)
  if (bufRef.current === null) bufRef.current = new TermBuffer()
  const buf = bufRef.current
  const snap = useSyncExternalStore(buf.subscribe, buf.getSnapshot, buf.getSnapshot)
  const [conn, setConn] = useState<Conn>('idle')
  const refreshShellRef = useRef(shell.refresh)
  refreshShellRef.current = shell.refresh

  // `lineCount` updates every poll; only its PRESENCE gates the stream, so
  // the effect keys on a boolean and reads the value from a ref — otherwise
  // each 10s poll would tear the stream down and replay the tail.
  const hasLineCount = lineCount !== undefined
  const lineCountRef = useRef(lineCount)
  if (lineCount !== undefined) lineCountRef.current = lineCount

  useEffect(() => {
    if (mode !== 'follow' || !hasLineCount) return
    let stopped = false
    let ctrl: AbortController | null = null

    // Hidden tab = 0 open streams (gate requirement). Abort on hide; the loop
    // waits for visibility and resumes from buf.lastLine at a NEW url.
    const onHide = () => {
      if (document.visibilityState === 'hidden') ctrl?.abort()
    }
    document.addEventListener('visibilitychange', onHide)

    const waitVisible = () =>
      new Promise<void>((resolve) => {
        if (document.visibilityState === 'visible') return resolve()
        const h = () => {
          if (document.visibilityState === 'visible') {
            document.removeEventListener('visibilitychange', h)
            resolve()
          }
        }
        document.addEventListener('visibilitychange', h)
      })

    void (async () => {
      let failures = 0
      while (!stopped) {
        await waitVisible()
        if (stopped) return
        ctrl = new AbortController()
        const since = buf.lastLine > 0 ? buf.lastLine : Math.max(0, (lineCountRef.current ?? 0) - 300)
        try {
          setConn('open')
          for await (const ev of openSse(K.shellStream(name, since), { signal: ctrl.signal })) {
            failures = 0
            if (ev.event === 'shell_event') {
              try {
                buf.push(JSON.parse(ev.data) as ShellEventWire)
              } catch {
                /* malformed frame — skip */
              }
            } else if (ev.event === 'shell_status') {
              void refreshShellRef.current()
            }
          }
        } catch {
          /* aborted (hide/unmount) or network failure — retry below */
        }
        if (stopped) return
        setConn('reconnecting')
        failures = Math.min(failures + 1, 4)
        await new Promise((r) => setTimeout(r, 1000 * 2 ** (failures - 1)))
      }
    })()

    return () => {
      stopped = true
      ctrl?.abort()
      document.removeEventListener('visibilitychange', onHide)
      setConn('idle')
    }
  }, [mode, name, hasLineCount, buf])

  const displayLines = useMemo(
    () => (hideNoise ? snap.lines.filter((l) => !isNoise(l.text)) : snap.lines),
    [snap, hideNoise]
  )

  /* -------------------------------------------------------- auto-scroll */

  const scrollRef = useRef<HTMLDivElement | null>(null)
  const atBottomRef = useRef(true)
  const [showJump, setShowJump] = useState(false)

  useEffect(() => {
    if (mode !== 'follow') return
    const el = scrollRef.current
    if (el && atBottomRef.current) el.scrollTop = el.scrollHeight
  }, [snap.version, displayLines, mode])

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight
    atBottomRef.current = distance < 40
    setShowJump(mode === 'follow' && distance > 200)
  }

  function jumpToBottom() {
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    atBottomRef.current = true
    setShowJump(false)
  }

  /* -------------------------------------------------------------- input */

  const [inputText, setInputText] = useState('')
  const [busyKey, setBusyKey] = useState<string | null>(null)

  async function send(text: string, opts: { appendEnter?: boolean; clear?: boolean; key: string }) {
    if (!text || busyKey) return
    setBusyKey(opts.key)
    const res = await run(() => sendShellKeys(name, text, { appendEnter: opts.appendEnter, waitMs: 400 }), {
      onError: (e) => push('warn', e.message),
    })
    setBusyKey(null)
    if (res !== undefined) {
      if (opts.clear) setInputText('')
      // Act-and-observe: the response carries the pane wait_ms after the
      // keystroke — surface it now instead of waiting for the next poll.
      if (res.screen && mode === 'screen') {
        void screen.mutate({ name, lines: 40, screen: res.screen }, { revalidate: false })
      }
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    void send(inputText, { appendEnter: true, clear: true, key: 'send' })
  }

  async function fitPane() {
    const el = scrollRef.current
    if (!el) return
    // Approximate mono glyph metrics from the current font size; clamped to
    // the API's tmux bounds. Good enough for "make the TUI repaint at my
    // phone's geometry", which is what /resize exists for.
    const cols = Math.min(500, Math.max(20, Math.floor(el.clientWidth / (fontSize * 0.602))))
    const rows = Math.min(200, Math.max(10, Math.floor(el.clientHeight / (fontSize * 1.45))))
    const ok = await run(() => resizeShell(name, cols, rows), { onError: (e) => push('warn', e.message) })
    if (ok !== undefined) push('ok', `Resized to ${cols}×${rows}`)
  }

  function stepFont(delta: number) {
    setFontPref(String(Math.min(FONT_MAX, Math.max(FONT_MIN, fontSize + delta))))
  }

  /* ------------------------------------------------------------- render */

  const toggleClass = (on: boolean) => (on ? 'border-accent text-accent' : undefined)

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {/* ------------------------------------------------------- header */}
      <div className="shrink-0 border-b border-line px-2.5 py-1.5">
        <Cluster nowrap className="items-center gap-2">
          <Chip tone={status === 'active' ? 'ok' : isStopped ? 'neutral' : 'accent'} className="shrink-0">
            {status ?? '…'}
          </Chip>
          {shell.data?.host && <Chip className="shrink-0">{shell.data.host}</Chip>}
          <code className="shrink-0 whitespace-nowrap font-mono text-micro text-ink-faint">
            {shell.data?.project_dir ?? name}
          </code>
          <span className="ml-auto shrink-0 whitespace-nowrap text-micro text-ink-faint">
            {relativeTime(shell.data?.last_activity_at)}
          </span>
        </Cluster>
        <Cluster nowrap className="mt-1.5 items-center gap-1.5">
          <Button
            variant={mode === 'screen' ? 'primary' : 'default'}
            className="shrink-0 whitespace-nowrap"
            onClick={() => setMode('screen')}
          >
            Screen
          </Button>
          <Button
            variant={mode === 'follow' ? 'primary' : 'default'}
            className="shrink-0 whitespace-nowrap"
            onClick={() => setMode('follow')}
          >
            Follow
          </Button>
          {mode === 'follow' && conn !== 'idle' && (
            <Chip tone={conn === 'open' ? 'ok' : 'warn'} className="shrink-0">
              {conn === 'open' ? 'live' : 'reconnecting'}
            </Chip>
          )}
          <span aria-hidden="true" className="mx-0.5 h-5 w-px shrink-0 bg-line" />
          <Button
            aria-pressed={wrapped}
            className={`shrink-0 whitespace-nowrap ${toggleClass(wrapped) ?? ''}`}
            onClick={() => setWrapPref(wrapped ? '0' : '1')}
          >
            Wrap
          </Button>
          <Button
            aria-label="Smaller terminal text"
            className="shrink-0"
            onClick={() => stepFont(-1)}
            disabled={fontSize <= FONT_MIN}
          >
            A−
          </Button>
          <Button
            aria-label="Larger terminal text"
            className="shrink-0"
            onClick={() => stepFont(1)}
            disabled={fontSize >= FONT_MAX}
          >
            A+
          </Button>
          {mode === 'follow' && (
            <>
              <Button
                aria-pressed={showTs}
                className={`shrink-0 whitespace-nowrap ${toggleClass(showTs) ?? ''}`}
                onClick={() => setShowTs((v) => !v)}
              >
                TS
              </Button>
              <Button
                aria-pressed={hideNoise}
                className={`shrink-0 whitespace-nowrap ${toggleClass(hideNoise) ?? ''}`}
                onClick={() => setHideNoise((v) => !v)}
              >
                Denoise
              </Button>
            </>
          )}
          {!isStopped && (
            <ConfirmButton
              label="Fit"
              confirmLabel="Resize shared tmux?"
              variant="default"
              className="shrink-0 whitespace-nowrap"
              onConfirm={fitPane}
            />
          )}
        </Cluster>
      </div>

      {/* ----------------------------------------------------- terminal */}
      <div className="relative min-h-0 flex-1">
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="h-full overflow-y-auto overscroll-contain bg-ground px-2.5 py-2 font-mono"
          style={{ fontSize: `${fontSize}px`, lineHeight: 1.45 }}
        >
          {mode === 'screen' ? (
            isStopped ? (
              snapshot.data ? (
                <>
                  <p className="m-0 mb-2 text-micro uppercase tracking-[0.1em] text-ink-faint">
                    Stored snapshot · {relativeTime(snapshot.data.ts)} (shell stopped)
                  </p>
                  <ScreenPane text={snapshot.data.content} wrapped={wrapped} />
                </>
              ) : snapshot.error ? (
                <Notice tone="info">No snapshot was stored before this shell stopped.</Notice>
              ) : (
                <Skeleton rows={6} />
              )
            ) : screen.data ? (
              <ScreenPane text={screen.data.screen} wrapped={wrapped} />
            ) : screen.error ? (
              <Notice tone="warn">
                <span className="wrap-anywhere">{screen.error.message}</span>
              </Notice>
            ) : (
              <Skeleton rows={6} />
            )
          ) : displayLines.length === 0 ? (
            <p className="m-0 font-sans text-prose text-ink-faint">
              {conn === 'open'
                ? hideNoise
                  ? 'Nothing yet — the noise filter is on; toggle Denoise if you expected output.'
                  : 'Nothing yet in the last 300 lines.'
                : 'Connecting…'}
            </p>
          ) : wrapped ? (
            displayLines.map((l) => <LineRow key={l.key} line={l} showTs={showTs} wrapped />)
          ) : (
            <ScrollX>
              <div className="w-max min-w-full">
                {displayLines.map((l) => (
                  <LineRow key={l.key} line={l} showTs={showTs} wrapped={false} />
                ))}
              </div>
            </ScrollX>
          )}
        </div>
        {showJump && (
          <Button
            variant="primary"
            onClick={jumpToBottom}
            className="absolute bottom-3 right-3 shadow-lg"
          >
            ↓ Latest
          </Button>
        )}
      </div>

      {/* ------------------------------------------------------ compose */}
      <div className="shrink-0 border-t border-line bg-panel px-2.5 pt-2 pb-[calc(var(--tabbar-h)+var(--sab)+0.5rem)] lg:pb-2.5">
        <form onSubmit={onSubmit} className="flex items-center gap-2">
          <Input
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            placeholder={isStopped ? 'Shell is stopped — input disabled' : 'Send to shell'}
            disabled={isStopped}
            enterKeyHint="send"
            autoCapitalize="none"
            autoCorrect="off"
            autoComplete="off"
            spellCheck={false}
            className="flex-1 font-mono coarse:[font-size:1rem]"
          />
          {/* Enabled while a send is in flight (busy shows on the button):
              disabling the input mid-send dropped keystrokes and dismissed
              the phone keyboard. */}
          <Button type="submit" variant="primary" busy={busyKey === 'send'} disabled={isStopped || !inputText}>
            Send
          </Button>
        </form>
        <Cluster nowrap className="mt-2 gap-1.5 pb-0.5">
          {SPECIAL_KEYS.map((k) => (
            <Button
              key={k.label}
              onClick={() => void send(k.text, { appendEnter: k.appendEnter, key: k.label })}
              busy={busyKey === k.label}
              disabled={isStopped}
              className="min-w-touch shrink-0 font-mono"
            >
              {k.label}
            </Button>
          ))}
        </Cluster>
      </div>

      <Toasts toasts={toasts} onDismiss={dismiss} />
    </div>
  )
}
