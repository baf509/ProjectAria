'use client'

import { useEffect, useRef, useState } from 'react'

/**
 * Two-tap inline confirmation button.
 *
 * Replaces window.confirm(), which mobile browsers (and some desktop
 * settings) silently suppress — the click then appears to do nothing, which
 * is exactly how the Ridge sleep button "didn't work". First tap arms the
 * button (label switches to confirmLabel, styling intensifies); a second tap
 * within 4s fires onConfirm; otherwise it disarms itself.
 */
export function ConfirmButton({
  label,
  confirmLabel,
  onConfirm,
  className = '',
  armedClassName = '',
  disabled = false,
}: {
  label: string
  confirmLabel?: string
  onConfirm: () => void | Promise<void>
  className?: string
  armedClassName?: string
  disabled?: boolean
}) {
  const [armed, setArmed] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  function handleClick() {
    if (!armed) {
      setArmed(true)
      timer.current = setTimeout(() => setArmed(false), 4000)
      return
    }
    if (timer.current) clearTimeout(timer.current)
    setArmed(false)
    void onConfirm()
  }

  return (
    <button
      disabled={disabled}
      onClick={handleClick}
      className={`${className} ${armed ? armedClassName || 'ring-1 ring-current' : ''}`}
    >
      {armed ? (confirmLabel || `Confirm: ${label}?`) : label}
    </button>
  )
}
