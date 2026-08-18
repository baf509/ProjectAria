'use client'

/**
 * ARIA - Know: workflows (list + run)
 *
 * The declarative fan-out engine's library view: run / dry-run / inspect /
 * delete, plus the raw-JSON create form the old dashboard had (kept — it is
 * the only browser surface for authoring a workflow). Status is a real GET
 * resource keyed on the selected workflow, not a one-shot fetch into state,
 * so it survives navigation and refreshes with the tier.
 */
import { useState } from 'react'
import { useResource, useAction } from '@/lib/swr'
import { K, runWorkflow, deleteWorkflow, createWorkflow } from '@/lib/api/endpoints'
import type { Workflow, WorkflowStatus } from '@/lib/api/types'
import { Card, Chip, Code, Text } from '@/components/ui/primitives'
import { Button, ConfirmButton, Disclosure, Field, Input, Textarea, Toasts } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Stack, Cluster, Columns, ScrollX } from '@/components/layout'
import { relativeTime } from '@/lib/time'
import { useKnowStats } from '@/features/know/knowStatus'
import { useToasts } from '@/features/know/useToasts'

const STEPS_PLACEHOLDER = '[{"action":"notify","params":{"detail":"hello","event_type":"info"}}]'

function WorkflowRow({
  workflow,
  onDone,
  onError,
}: {
  workflow: Workflow
  onDone: (t: string) => void
  onError: (t: string) => void
}) {
  const run = useAction()
  const [busy, setBusy] = useState<string | null>(null)
  const [showStatus, setShowStatus] = useState(false)
  // Mounted only while open, so the segment's base load stays at one request.
  const status = useResource<WorkflowStatus>(showStatus ? K.workflowStatus(workflow._id) : null, { tier: 'fast' })

  async function start(dry: boolean) {
    setBusy(dry ? 'dry' : 'run')
    const ok = await run(() => runWorkflow(workflow._id, dry), {
      invalidate: ['/tasks'],
      onError: (e) => onError(e.message),
    })
    setBusy(null)
    if (ok !== undefined) onDone(`${dry ? 'Dry run' : 'Run'} started: ${workflow.name}`)
  }

  return (
    <li className="border-b border-line py-2.5 last:border-b-0">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 font-sans text-prose text-ink">{workflow.name}</span>
        <Chip>{workflow.steps?.length ?? 0} steps</Chip>
        <span className="shrink-0 text-micro text-ink-faint">{relativeTime(workflow.updated_at)}</span>
      </div>
      {workflow.description && <Text clamp={2} className="mt-1">{workflow.description}</Text>}
      <Cluster className="mt-2">
        <Button variant="primary" busy={busy === 'run'} onClick={() => void start(false)}>
          Run
        </Button>
        <Button busy={busy === 'dry'} onClick={() => void start(true)}>
          Dry run
        </Button>
        <Button onClick={() => setShowStatus((s) => !s)} aria-expanded={showStatus}>
          {showStatus ? 'Hide status' : 'Status'}
        </Button>
        <ConfirmButton
          label="Delete"
          onConfirm={async () => {
            const ok = await run(() => deleteWorkflow(workflow._id), {
              invalidate: ['/workflows'],
              onError: (e) => onError(e.message),
            })
            if (ok !== undefined) onDone(`Deleted ${workflow.name}`)
          }}
        />
      </Cluster>
      {showStatus && (
        <div className="mt-2 rounded-sm border border-line bg-panel-2 p-2.5">
          <Async r={status} skeletonRows={2}>
            {(d) => (
              <ScrollX>
                <pre className="m-0 text-micro text-ink-dim">
                  {JSON.stringify((d.runs ?? []).slice(0, 3), null, 2) || 'no runs yet'}
                </pre>
              </ScrollX>
            )}
          </Async>
        </div>
      )}
    </li>
  )
}

export default function WorkflowsPage() {
  const toasts = useToasts()
  const runAction = useAction()
  const workflows = useResource<Workflow[]>(K.workflows, { tier: 'lazy' })
  const [form, setForm] = useState({ name: '', description: '', steps: STEPS_PLACEHOLDER })
  const [busy, setBusy] = useState(false)

  useKnowStats([{ label: 'WORKFLOWS', value: workflows.data?.length ?? '—' }])

  async function create() {
    if (!form.name.trim()) {
      toasts.warn('Workflow name is required.')
      return
    }
    let steps: unknown[]
    try {
      steps = JSON.parse(form.steps)
      if (!Array.isArray(steps)) throw new Error('steps must be a JSON array')
    } catch (e) {
      toasts.warn(`Invalid steps JSON: ${e instanceof Error ? e.message : String(e)}`)
      return
    }
    setBusy(true)
    const ok = await runAction(
      () => createWorkflow({ name: form.name.trim(), description: form.description.trim() || undefined, steps }),
      { invalidate: ['/workflows'], onError: (e) => toasts.warn(e.message) }
    )
    setBusy(false)
    if (ok !== undefined) {
      toasts.ok(`Created workflow ${form.name.trim()}`)
      setForm({ name: '', description: '', steps: STEPS_PLACEHOLDER })
    }
  }

  return (
    <>
      <Columns lg={2}>
        <Card title="Workflow library">
          <Async
            r={workflows}
            skeletonRows={4}
            isEmpty={(d) => d.length === 0}
            empty="No workflows defined. Create one on the right."
          >
            {(items) => (
              <ul className="m-0 list-none p-0">
                {items.map((wf) => (
                  <WorkflowRow key={wf._id} workflow={wf} onDone={toasts.ok} onError={toasts.warn} />
                ))}
              </ul>
            )}
          </Async>
        </Card>

        <Card title="Create workflow" hint="raw step JSON — the engine's native format">
          <Stack gap="sm">
            <Field label="Name">
              <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Workflow name" className="coarse:text-title" />
            </Field>
            <Field label="Description">
              <Textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                rows={2}
                className="coarse:text-title"
              />
            </Field>
            <Field
              label="Steps (JSON)"
              hint={
                <>
                  Supports <Code>{'{{steps.0.response}}'}</Code> interpolation, <Code>depends_on</Code> arrays and{' '}
                  <Code>condition</Code> gates.
                </>
              }
            >
              <Textarea
                value={form.steps}
                onChange={(e) => setForm({ ...form, steps: e.target.value })}
                rows={10}
                className="font-mono text-micro coarse:text-title"
                spellCheck={false}
              />
            </Field>
            <Button variant="primary" busy={busy} onClick={() => void create()} className="self-start">
              Create workflow
            </Button>
          </Stack>
        </Card>
      </Columns>
      <Toasts toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </>
  )
}
