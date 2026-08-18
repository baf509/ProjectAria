'use client'

/**
 * ARIA - session-only admin key
 *
 * A handful of routes are gated by `X-Admin-Key` (`require_admin` in
 * api/deps.py): the killswitch, `PUT /agents`, `set_llm_route`, the guard merge,
 * policy accept. The reasoning is in ARIA's own docs — anything running as `ben`
 * can read `API_KEY` out of `.env`, so `API_KEY` cannot be what stands between
 * an agent and an irreversible action.
 *
 * Consequence for this UI: without a key, "Serve this" and route pinning fail
 * with a 403 that reads like a bug. So the operator can type the key for the
 * session — held in module memory only, NEVER localStorage or a cookie, and
 * gone on reload. That is deliberate: a key persisted in the browser is a key
 * any script on this origin can use, which defeats the point of the gate.
 */
import { useState } from 'react'
import { hasAdminKey, setAdminKey } from '@/lib/http'
import { Button, Field, Input } from '@/components/ui/controls'

export function AdminKeyControl() {
  const [value, setValue] = useState('')
  const [armed, setArmed] = useState(hasAdminKey())

  if (armed) {
    return (
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="text-micro uppercase tracking-[0.08em] text-ink-faint">Admin key</span>
        <span className="text-micro text-live">active this session</span>
        <Button
          className="ml-auto"
          onClick={() => {
            setAdminKey(null)
            setArmed(false)
          }}
        >
          Clear
        </Button>
      </div>
    )
  }

  return (
    <form
      className="flex min-w-0 flex-col gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        if (!value.trim()) return
        setAdminKey(value.trim())
        setValue('')
        setArmed(true)
      }}
    >
      <Field
        label="Admin key"
        hint="Needed for Serve this, route pinning and agent edits. Held in memory for this session only."
      >
        <Input
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="X-Admin-Key"
          autoComplete="off"
        />
      </Field>
      <Button type="submit" variant="primary" disabled={!value.trim()} className="self-start">
        Use for this session
      </Button>
    </form>
  )
}
