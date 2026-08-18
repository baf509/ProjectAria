'use client'

/**
 * ARIA - Interactive primitives (client)
 *
 * Everything that needs state or a DOM API. Kept apart from primitives.tsx so
 * the static ones stay server-renderable.
 *
 * Sizes come from `--control-h`, which is 44px under `pointer: coarse` and 32px
 * with a mouse — the audit measured every button in the app at 31px, and the
 * previous fix pattern (`py-3 sm:py-1.5` per button) had to be repeated at 21
 * call sites and drifted anyway.
 */
import {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
  useEffect,
  useId,
  useRef,
  useState,
} from 'react'

const cx = (...p: Array<string | false | undefined>) => p.filter(Boolean).join(' ')

/* ------------------------------------------------------------------ button */

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'default' | 'primary' | 'danger' | 'ghost'
  size?: 'default' | 'compact'
  busy?: boolean
}

export function Button({
  variant = 'default',
  size = 'default',
  busy,
  children,
  className = '',
  ...rest
}: BtnProps) {
  const base =
    // `shrink-0 whitespace-nowrap`: the shell's `min-width: 0` base rule lets a
    // flex item shrink below its content, which squeezed button labels into
    // two lines ("SCRE EN"). Controls are the one thing that must keep their
    // intrinsic width — text is what wraps, not the control around it.
    'relative inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-sm px-3 text-micro uppercase tracking-[0.08em] transition-colors ' +
    'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent ' +
    'disabled:cursor-not-allowed disabled:opacity-40 [touch-action:manipulation]'
  const sizes = {
    default: 'min-h-control min-w-control',
    compact: 'min-h-8 py-1',
  }
  const variants = {
    default: 'border border-line bg-transparent text-ink hover:border-ink-faint',
    primary: 'border border-accent bg-accent font-semibold text-accent-ink hover:brightness-110',
    danger: 'border border-gone bg-transparent text-gone hover:bg-gone/10',
    ghost: 'border border-transparent bg-transparent text-ink-dim hover:bg-panel-2 hover:text-ink',
  }
  return (
    <button
      {...rest}
      aria-busy={busy || undefined}
      disabled={rest.disabled || busy}
      className={cx(base, sizes[size], variants[variant], className)}
    >
      {/* The label stays mounted while busy: swapping it for '···' (the old
          behaviour) changed the button's width mid-action and moved whatever
          the thumb was aimed at next. */}
      <span className={busy ? 'invisible' : undefined}>{children}</span>
      {busy && (
        <span className="absolute inset-0 grid place-items-center" aria-hidden="true">
          <span className="h-3 w-3 animate-spin rounded-full border border-current border-t-transparent" />
        </span>
      )}
    </button>
  )
}

export function IconButton({
  label,
  children,
  className = '',
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; children: ReactNode }) {
  return (
    <button
      {...rest}
      aria-label={label}
      title={label}
      className={cx(
        'inline-grid min-h-control min-w-control place-items-center rounded-sm text-ink-dim transition-colors',
        'hover:bg-panel-2 hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent',
        'disabled:cursor-not-allowed disabled:opacity-40 [touch-action:manipulation]',
        className
      )}
    >
      {children}
    </button>
  )
}

/**
 * Two-tap inline confirmation. Replaces window.confirm(), which mobile browsers
 * silently suppress — the click then appears to do nothing.
 */
export function ConfirmButton({
  label,
  confirmLabel,
  onConfirm,
  variant = 'danger',
  disabled = false,
  className = '',
}: {
  label: string
  confirmLabel?: string
  onConfirm: () => void | Promise<void>
  variant?: BtnProps['variant']
  disabled?: boolean
  className?: string
}) {
  const [armed, setArmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  async function handleClick() {
    if (!armed) {
      setArmed(true)
      timer.current = setTimeout(() => setArmed(false), 4000)
      return
    }
    if (timer.current) clearTimeout(timer.current)
    setArmed(false)
    setBusy(true)
    try {
      await onConfirm()
    } finally {
      setBusy(false)
    }
  }

  return (
    <Button
      variant={variant}
      busy={busy}
      disabled={disabled}
      onClick={handleClick}
      className={cx(armed && 'ring-1 ring-current', className)}
    >
      {armed ? confirmLabel || `Confirm ${label}?` : label}
    </Button>
  )
}

/* ------------------------------------------------------------------- field */

export function Field({
  label,
  hint,
  children,
  className = '',
}: {
  label: string
  hint?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <label className={cx('flex min-w-0 flex-col gap-1', className)}>
      <span className="text-micro uppercase tracking-[0.08em] text-ink-faint">{label}</span>
      {children}
      {hint && <span className="text-micro text-ink-faint">{hint}</span>}
    </label>
  )
}

/**
 * `coarse:text-title` (16px) is load-bearing, not decoration: iOS zooms the
 * page into any focused control under 16px and does NOT zoom back out, which
 * was one of the two mechanisms behind "I have to zoom". globals.css has a
 * base-layer rule saying the same thing, but a utility class outranks
 * @layer base — so without the variant here every field silently lost the
 * floor. (Found independently by three refits; do not "simplify" it away.)
 */
const fieldBase =
  'w-full min-w-0 min-h-control rounded-sm border border-line bg-panel-2 px-2.5 py-1.5 text-body coarse:text-title text-ink ' +
  'placeholder:text-ink-faint focus:border-accent focus:outline-none'

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cx(fieldBase, props.className)} />
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={cx(fieldBase, props.className)} />
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cx(fieldBase, 'resize-y', props.className)} />
}

/* -------------------------------------------------------------- disclosure */

/**
 * A real expand, not a hover tooltip. `title=` reveals nothing on touch, which
 * is why every truncated alert title in the old Inbox was unreadable on a phone.
 */
export function Disclosure({
  summary,
  children,
  defaultOpen = false,
  lazy = false,
  onToggle,
  className = '',
}: {
  summary: ReactNode
  children: ReactNode
  defaultOpen?: boolean
  /**
   * A <details> renders its children whether or not it is open, so anything
   * that fetches on mount would fetch immediately. `lazy` withholds them until
   * first open — which is what lets a panel like "N stopped shells" cost
   * nothing until someone asks for it.
   */
  lazy?: boolean
  onToggle?: (open: boolean) => void
  className?: string
}) {
  const [opened, setOpened] = useState(defaultOpen)
  return (
    <details
      className={cx('group min-w-0', className)}
      open={defaultOpen}
      onToggle={(e) => {
        const isOpen = (e.currentTarget as HTMLDetailsElement).open
        if (isOpen) setOpened(true)
        onToggle?.(isOpen)
      }}
    >
      <summary className="flex min-h-control cursor-pointer list-none items-center gap-2 text-body marker:content-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent">
        <span aria-hidden="true" className="shrink-0 text-ink-faint transition-transform group-open:rotate-90">
          ▸
        </span>
        <span className="min-w-0 flex-1">{summary}</span>
      </summary>
      <div className="mt-2 min-w-0">{lazy && !opened ? null : children}</div>
    </details>
  )
}

/* ------------------------------------------------------------------- sheet */

/**
 * Bottom sheet on touch, centred dialog from lg. Native <dialog> so focus
 * trapping, Esc and inertness come from the platform rather than 200 lines of
 * focus management.
 */
export function Sheet({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const labelId = useId()

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (open && !el.open) el.showModal()
    if (!open && el.open) el.close()
  }, [open])

  return (
    <dialog
      ref={ref}
      aria-labelledby={labelId}
      onClose={onClose}
      onClick={(e) => {
        if (e.target === ref.current) onClose()
      }}
      className={cx(
        'w-full max-w-full border border-line bg-panel p-0 text-ink backdrop:bg-ground/70',
        'm-0 mt-auto max-h-[85dvh] rounded-t pb-sab',
        'lg:m-auto lg:max-w-lg lg:rounded lg:pb-0'
      )}
    >
      <div className="flex min-h-control items-center gap-3 border-b border-line px-3.5 py-2.5">
        <h2 id={labelId} className="text-micro font-medium uppercase tracking-[0.16em] text-ink-faint">
          {title}
        </h2>
        <button
          onClick={onClose}
          aria-label="Close"
          className="ml-auto inline-grid min-h-control min-w-control place-items-center rounded-sm text-ink-dim hover:bg-panel-2 hover:text-ink"
        >
          ✕
        </button>
      </div>
      <div className="max-h-[70dvh] overflow-y-auto p-3.5">{children}</div>
    </dialog>
  )
}

/* ------------------------------------------------------------------- toast */

export type Toast = { id: number; tone: 'ok' | 'warn'; text: string }

export function Toasts({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  return (
    <div
      aria-live="polite"
      className="pointer-events-none fixed inset-x-0 bottom-[calc(var(--tabbar-h)+var(--sab)+0.5rem)] z-toast flex flex-col items-center gap-2 px-gutter lg:bottom-4"
    >
      {toasts.map((t) => (
        <button
          key={t.id}
          onClick={() => onDismiss(t.id)}
          className={cx(
            'pointer-events-auto w-full max-w-md rounded-sm border px-3 py-2 text-left font-sans text-prose shadow-lg',
            t.tone === 'ok' ? 'border-live/50 bg-live/10 text-ink' : 'border-gone/50 bg-gone/10 text-ink'
          )}
        >
          {t.text}
        </button>
      ))}
    </div>
  )
}

/* --------------------------------------------------------------- tab strip */

export function TabStrip({
  items,
  active,
  onSelect,
}: {
  items: Array<{ key: string; label: string; count?: number }>
  active: string
  onSelect: (key: string) => void
}) {
  return (
    <div
      data-scroll-x
      className="flex snap-x gap-1.5 overflow-x-auto overscroll-x-contain pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
    >
      {items.map((it) => (
        <button
          key={it.key}
          onClick={() => onSelect(it.key)}
          aria-current={active === it.key ? 'page' : undefined}
          className={cx(
            'min-h-control shrink-0 snap-start whitespace-nowrap rounded-sm border px-3 text-micro uppercase tracking-[0.08em] transition-colors',
            active === it.key
              ? 'border-accent bg-accent/10 text-accent'
              : 'border-line bg-panel text-ink-dim hover:border-ink-faint hover:text-ink'
          )}
        >
          {it.label}
          {it.count !== undefined && <span className="tnum ml-1.5 text-ink-faint">{it.count}</span>}
        </button>
      ))}
    </div>
  )
}
