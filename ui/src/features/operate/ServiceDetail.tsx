'use client'

/**
 * ARIA - Operate: non-LLM service detail (/operate/services/[slug])
 *
 * New as a routed page (the old UI had only list rows with a 26px start/stop
 * strip). The semantics that shape it: `expected_state` is what makes
 * "stopped" mean opposite things — a stopped on_demand service is normal, a
 * stopped always_up one is an incident — and the server already folded that
 * into `healthy`, so this renders the verdict rather than re-deriving it.
 *
 * Stop is ALWAYS two-tap for `always_up` services: several are shared
 * cross-project (mongod also backs AgentBenchPlatform; mongot is shared), so
 * a one-tap stop from a phone is exactly the accident the registry notes warn
 * about.
 */
import Link from 'next/link'
import { useEffect, useState } from 'react'
import type { ServiceFull, ServicesResponse } from '@/lib/api/types'
import { Card, Chip, Code, KeyValue, Notice, StateChip, Text, type KeyValueItem } from '@/components/ui/primitives'
import { Button, ConfirmButton, Toasts } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Cluster, Stack } from '@/components/layout'
import { useAction, useResource } from '@/lib/swr'
import { K, serviceAction } from '@/lib/api/endpoints'
import { normalizeState } from '@/components/ui/primitives'
import { formatDuration } from '@/lib/time'
import { useToasts } from './lib'

type Kind = 'start' | 'stop'

function Body({ service }: { service: ServiceFull }) {
  const run = useAction()
  const { toasts, push, dismiss } = useToasts()
  const [busy, setBusy] = useState<Kind | null>(null)
  const [pending, setPending] = useState<{ kind: Kind; at: number } | null>(null)
  // Action refusals persist across successful polls — separate slots.
  const [actionError, setActionError] = useState<string | null>(null)

  const st = normalizeState(service.state)
  const running = st === 'running'

  useEffect(() => {
    if (!pending) return
    if ((pending.kind === 'start' && running) || (pending.kind === 'stop' && !running)) {
      setPending(null)
      push('ok', `${service.slug} is ${running ? 'up' : 'stopped'}`)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running])

  async function act(kind: Kind) {
    setBusy(kind)
    const ok = await run(() => serviceAction(service.slug, kind), {
      invalidate: ['/infrastructure/services'],
      onError: (e) => setActionError(`${kind}: ${typeof e.detail === 'string' ? e.detail : e.message}`),
    })
    setBusy(null)
    if (ok !== undefined) {
      setActionError(null)
      setPending({ kind, at: Date.now() })
      push('ok', `${kind} requested`)
    }
  }

  const items: KeyValueItem[] = [
    { k: 'Kind', v: service.kind ?? '—', kind: 'prose' },
    { k: 'State', v: service.state ?? '—', kind: 'prose' },
    {
      k: 'Expected',
      v: service.expected_state === 'always_up' ? 'always up — down is an incident' : 'on demand — stopped is normal',
      kind: 'prose',
    },
    { k: 'Port', v: service.port ?? '—', kind: 'num' },
    ...(service.unit ? [{ k: 'Systemd unit', v: service.unit, kind: 'ident' as const }] : []),
    ...(service.container ? [{ k: 'Container', v: service.container, kind: 'ident' as const }] : []),
    ...(service.compose_file ? [{ k: 'Compose file', v: service.compose_file, kind: 'ident' as const }] : []),
    ...(service.depends_on?.length ? [{ k: 'Depends on', v: service.depends_on.join(' · '), kind: 'ident' as const }] : []),
  ]

  return (
    <Stack>
      <Card
        title="Service"
        actions={
          service.manageable === false ? undefined : running ? (
            service.expected_state === 'always_up' ? (
              <ConfirmButton label="Stop" confirmLabel="Stop an always-up service?" onConfirm={() => act('stop')} />
            ) : (
              <Button busy={busy === 'stop'} onClick={() => act('stop')}>
                Stop
              </Button>
            )
          ) : (
            <Button variant="primary" busy={busy === 'start'} onClick={() => act('start')}>
              Start
            </Button>
          )
        }
      >
        <Stack gap="sm">
          <Cluster>
            <h3 className="m-0 min-w-0 wrap-anywhere font-mono text-title font-semibold">{service.slug}</h3>
            <StateChip state={service.healthy ? (running ? 'running' : 'external') : 'failed'} note={service.healthy ? undefined : 'expected up'} />
            {service.expected_state && <Chip>{service.expected_state}</Chip>}
            {service.needs_review && <Chip tone="warn">needs review</Chip>}
          </Cluster>

          {service.description && <Text clamp={3}>{service.description}</Text>}

          {actionError && (
            <Notice tone="warn">
              <div className="flex flex-wrap items-center gap-2">
                <span className="min-w-0 flex-1 wrap-anywhere">{actionError}</span>
                <Button onClick={() => setActionError(null)}>Dismiss</Button>
              </div>
            </Notice>
          )}

          {pending && (
            <Notice tone="info">
              <b>{pending.kind} requested</b> · {formatDuration((Date.now() - pending.at) / 1000)} ago — the
              next poll confirms.
            </Notice>
          )}

          {service.manageable === false && (
            <Notice tone="info">
              <b>Locked.</b> ARIA must not start or stop this one
              {service.slug === 'aria-api' ? ' (it would restart itself mid-request)' : ''}.
            </Notice>
          )}

          {service.needs_review && (
            <Notice tone="warn">
              Its <Code>expected_state</Code> was inferred from observed state, not confirmed — until it is,
              an outage here will not alert.
            </Notice>
          )}

          {service.notes && (
            <Notice tone="info">
              <span className="wrap-anywhere">{service.notes}</span>
            </Notice>
          )}

          <KeyValue layout="stack" items={items} />
        </Stack>
      </Card>

      <p className="m-0 text-micro text-ink-faint">
        <Link href="/operate" className="text-accent underline underline-offset-2" data-inline>
          Back to the fleet
        </Link>
      </p>
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </Stack>
  )
}

export function ServiceDetail({ slug }: { slug: string }) {
  const services = useResource<ServicesResponse>(K.services, { tier: 'slow' })
  return (
    <Async r={services} skeletonRows={6}>
      {(d) => {
        const service = d.services.find((s) => s.slug === slug)
        if (!service)
          return (
            <Notice tone="warn">
              No service named <Code>{slug}</Code>.{' '}
              <Link href="/operate" className="text-accent underline underline-offset-2">
                Back to the fleet
              </Link>
            </Notice>
          )
        return <Body service={service} />
      }}
    </Async>
  )
}
