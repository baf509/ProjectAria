'use client'

/**
 * ARIA - minimal markdown for assistant replies
 *
 * In-house on purpose: react-markdown + remark-gfm would be the largest chunk
 * on the route for four constructs the local models actually emit — paragraphs,
 * lists, inline code, and fenced code blocks. Everything renders as React
 * children (never dangerouslySetInnerHTML), so model output cannot inject
 * markup.
 *
 * Fenced code lives in a ScrollX with `pre.pre` (the sanctioned opt-out from
 * the global `pre { white-space: pre-wrap }`), so a 200-column diff scrolls
 * inside its own box instead of widening the thread — the exact defect class
 * the rebuild's overflow gate measures.
 *
 * Single-asterisk italics are deliberately not parsed: models emit bare `*` in
 * shell globs and math constantly, and a false positive silently eats
 * characters. Bold, inline code and explicit links only.
 */
import { memo, useState, type ReactNode } from 'react'
import { Check, Copy } from 'lucide-react'
import { ScrollX } from '@/components/layout'
import { IconButton } from '@/components/ui/controls'

/* ------------------------------------------------------------------ inline */

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[[^\]\n]+\]\((?:https?:\/\/)[^)\s]+\))/g

function renderInline(text: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  INLINE.lastIndex = 0
  while ((m = INLINE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith('`')) {
      out.push(
        <code key={m.index} className="rounded-sm bg-panel-2 px-1 font-mono text-label text-ink">
          {tok.slice(1, -1)}
        </code>
      )
    } else if (tok.startsWith('**')) {
      out.push(<strong key={m.index}>{tok.slice(2, -2)}</strong>)
    } else {
      const label = tok.slice(1, tok.indexOf(']('))
      const href = tok.slice(tok.indexOf('](') + 2, -1)
      out.push(
        // data-inline: an in-text link is exempt from the 44px rule (WCAG 2.5.8)
        <a key={m.index} href={href} target="_blank" rel="noreferrer" data-inline className="text-accent underline underline-offset-2">
          {label}
        </a>
      )
    }
    last = m.index + tok.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

/* -------------------------------------------------------------- code block */

function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="group relative min-w-0 rounded-sm border border-line bg-panel-2">
      <div className="flex min-w-0 items-center gap-2 border-b border-line px-2 py-0.5">
        <span className="text-micro uppercase tracking-[0.08em] text-ink-faint">{lang || 'code'}</span>
        <IconButton
          label={copied ? 'Copied' : 'Copy code'}
          className="ml-auto"
          onClick={() => {
            void navigator.clipboard?.writeText(code).then(() => {
              setCopied(true)
              setTimeout(() => setCopied(false), 1500)
            })
          }}
        >
          {copied ? <Check size={15} aria-hidden="true" className="text-live" /> : <Copy size={15} aria-hidden="true" />}
        </IconButton>
      </div>
      <ScrollX>
        <pre className="pre m-0 px-2.5 py-2 font-mono text-label leading-relaxed text-ink">{code}</pre>
      </ScrollX>
    </div>
  )
}

/* ------------------------------------------------------------------ blocks */

type Block =
  | { kind: 'code'; lang?: string; code: string }
  | { kind: 'ul'; items: string[] }
  | { kind: 'ol'; items: string[] }
  | { kind: 'quote'; lines: string[] }
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'p'; text: string }

function parse(src: string): Block[] {
  const lines = src.replace(/\r\n/g, '\n').split('\n')
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const fence = line.match(/^```(\S*)\s*$/)
    if (fence) {
      const code: string[] = []
      i++
      while (i < lines.length && !/^```\s*$/.test(lines[i])) code.push(lines[i++])
      i++ // closing fence (or EOF — an unclosed fence still renders as code)
      blocks.push({ kind: 'code', lang: fence[1] || undefined, code: code.join('\n') })
      continue
    }
    if (/^\s*$/.test(line)) {
      i++
      continue
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/)
    if (heading) {
      blocks.push({ kind: 'heading', level: heading[1].length, text: heading[2] })
      i++
      continue
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*]\s+/, ''))
      blocks.push({ kind: 'ul', items })
      continue
    }
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*\d+[.)]\s+/, ''))
      blocks.push({ kind: 'ol', items })
      continue
    }
    if (/^>\s?/.test(line)) {
      const quote: string[] = []
      while (i < lines.length && /^>\s?/.test(lines[i])) quote.push(lines[i++].replace(/^>\s?/, ''))
      blocks.push({ kind: 'quote', lines: quote })
      continue
    }
    // Paragraph: consume until a blank line or the start of another construct.
    const para: string[] = [line]
    i++
    while (
      i < lines.length &&
      !/^\s*$/.test(lines[i]) &&
      !/^```/.test(lines[i]) &&
      !/^\s*[-*]\s+/.test(lines[i]) &&
      !/^\s*\d+[.)]\s+/.test(lines[i]) &&
      !/^#{1,4}\s+/.test(lines[i]) &&
      !/^>\s?/.test(lines[i])
    ) {
      para.push(lines[i++])
    }
    blocks.push({ kind: 'p', text: para.join('\n') })
  }
  return blocks
}

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  const blocks = parse(text)
  return (
    <div className="flex min-w-0 flex-col gap-2 font-sans text-prose text-ink">
      {blocks.map((b, idx) => {
        switch (b.kind) {
          case 'code':
            return <CodeBlock key={idx} code={b.code} lang={b.lang} />
          case 'heading':
            return (
              <p key={idx} className={`m-0 font-semibold ${b.level <= 2 ? 'text-title' : 'text-prose'}`}>
                {renderInline(b.text)}
              </p>
            )
          case 'ul':
            return (
              <ul key={idx} className="m-0 flex list-disc flex-col gap-1 pl-5">
                {b.items.map((it, j) => (
                  <li key={j} className="min-w-0">{renderInline(it)}</li>
                ))}
              </ul>
            )
          case 'ol':
            return (
              <ol key={idx} className="m-0 flex list-decimal flex-col gap-1 pl-5">
                {b.items.map((it, j) => (
                  <li key={j} className="min-w-0">{renderInline(it)}</li>
                ))}
              </ol>
            )
          case 'quote':
            return (
              <blockquote key={idx} className="m-0 border-l-2 border-line pl-3 text-ink-dim">
                {renderInline(b.lines.join('\n'))}
              </blockquote>
            )
          default:
            return (
              <p key={idx} className="m-0 whitespace-pre-wrap">
                {renderInline(b.text)}
              </p>
            )
        }
      })}
    </div>
  )
})
