/**
 * ARIA - SSE over fetch
 *
 * A fetch-body reader rather than `EventSource`, for three reasons the audit
 * measured:
 *  1. `EventSource` cannot send headers, which is why the shells stream had the
 *     master API key in its query string.
 *  2. `EventSource` reconnects to its FIXED url — after an iOS background/
 *     foreground cycle it replays from whatever `since_line` it was opened
 *     with. Resume needs a new URL.
 *  3. Chat streams over POST, which `EventSource` cannot do at all.
 */

export type SseEvent = { event: string; data: string; id?: string }

export async function* openSse(
  path: string,
  init: { method?: 'GET' | 'POST'; body?: unknown; signal?: AbortSignal } = {}
): AsyncGenerator<SseEvent> {
  const { method = 'GET', body, signal } = init
  const res = await fetch('/api/v1' + path, {
    method,
    signal,
    headers: {
      Accept: 'text/event-stream',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => '')
    throw new Error(text || `stream failed (${res.status})`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let sep: number
      // Events are separated by a blank line; \r\n is tolerated.
      while ((sep = buffer.search(/\r?\n\r?\n/)) !== -1) {
        const raw = buffer.slice(0, sep)
        buffer = buffer.slice(sep + (buffer[sep] === '\r' ? 4 : 2))

        let event = 'message'
        let id: string | undefined
        const dataLines: string[] = []
        for (const line of raw.split(/\r?\n/)) {
          if (!line || line.startsWith(':')) continue
          const idx = line.indexOf(':')
          const field = idx === -1 ? line : line.slice(0, idx)
          const val = idx === -1 ? '' : line.slice(idx + 1).replace(/^ /, '')
          if (field === 'event') event = val
          else if (field === 'data') dataLines.push(val)
          else if (field === 'id') id = val
        }
        if (dataLines.length) yield { event, data: dataLines.join('\n'), id }
      }
    }
  } finally {
    reader.cancel().catch(() => {})
  }
}
