'use client'

import { useState } from 'react'
import { AppShell } from '@/components/shell/AppShell'
import { Card, Chip, Text } from '@/components/ui/primitives'
import { Button, Disclosure, Field, Input, Textarea } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Stack, Cluster, ScrollX } from '@/components/layout'
import { api } from '@/lib/http'
import { useResource } from '@/lib/swr'
import { middleTruncate } from '@/lib/format'

type Task = {
  id: string; description: string; state: string; attempt_count: number
  accepted_commit: string | null; handoff: string; acceptance_criteria: string[]
  dependencies: string[]; check_ids: string[]; allowed_paths: string[]; human_review: string[]
}
type Attempt = {
  id: string; task_id: string; session_id: string | null; outcome: string
  execution_outcome: string | null; verification_outcome: string | null
  candidate_revision: string | null; checks: Record<string, unknown>[]; changes: string[]
}
type Run = {
  _id: string; version: number; project: string; state: string; stop_reason: string | null
  specification: string; plan: unknown; tasks: Task[]; attempts: Attempt[]
  accepted_revision: string; final_revision: string | null; human_review: string[]
  limits: Record<string, unknown>; usage: Record<string, unknown>; metrics: Record<string, unknown>
  events: Record<string, unknown>[]; final_evidence: Record<string, unknown>[]
}
type Policy = { enabled: boolean; projects: Record<string, { check_ids: string[]; allowed_backends: string[] }> }
type Log = { _id: string; kind: string; content: string }

function Json({ value }: { value: unknown }) {
  return <ScrollX><pre className="m-0 whitespace-pre-wrap break-words text-micro text-ink-dim">{JSON.stringify(value, null, 2)}</pre></ScrollX>
}

export default function LoopPage() {
  const [selected, setSelected] = useState<string | null>(null)
  const [project, setProject] = useState('')
  const [specification, setSpecification] = useState('')
  const [backend, setBackend] = useState('llamacpp')
  const [model, setModel] = useState('aria-resident')
  const [plan, setPlan] = useState('')
  const [planEdit, setPlanEdit] = useState('')
  const [planVersion, setPlanVersion] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [logAttempt, setLogAttempt] = useState<string | null>(null)
  const [logOffset, setLogOffset] = useState(0)
  const runs = useResource<Run[]>('/loop/runs', { tier: 'fast' })
  const policy = useResource<Policy>('/loop/policy', { tier: 'lazy' })
  const detail = useResource<Run>(selected ? `/loop/runs/${selected}` : null, { tier: 'fast' })
  const logs = useResource<Log[]>(selected && logAttempt ? `/loop/runs/${selected}/logs?attempt_id=${logAttempt}&offset=${logOffset}&limit=10` : null, { tier: 'slow' })

  async function act(operation: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await operation()
      await Promise.all([runs.mutate(), detail.mutate()])
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }

  return <AppShell title="Loops" back={{ href: '/supervise', label: 'Supervise' }}>
    <Stack>
      <Text>Turn an approved plan into verified local checkpoints. Each attempt works on one task in a fresh session. Review and approve the plan before starting.</Text>
      {error && <p role="alert" className="text-micro text-gone">{error}</p>}
      <Card>
        <Disclosure summary="Create a run">
          <Stack>
            <Async r={policy}>{p => <Text>{p.enabled ? `Configured projects: ${Object.keys(p.projects).join(', ') || 'none'}` : 'Loop is disabled. Configure the operator policy and enable Loop on the API.'}</Text>}</Async>
            <Field label="Project"><Input value={project} onChange={e => setProject(e.target.value)} placeholder="Registered project ID" /></Field>
            {policy.data?.projects[project] && <Text>Approved checks: {policy.data.projects[project].check_ids.join(', ')}</Text>}
            <Field label="Specification"><Textarea rows={5} value={specification} onChange={e => setSpecification(e.target.value)} /></Field>
            <Cluster>
              <Field label="Provider backend"><Input value={backend} onChange={e => setBackend(e.target.value)} /></Field>
              <Field label="Model"><Input value={model} onChange={e => setModel(e.target.value)} /></Field>
            </Cluster>
            <Field label="Existing plan JSON (optional)"><Textarea rows={8} value={plan} onChange={e => setPlan(e.target.value)} placeholder='{"version":1,"tasks":[...]}' /></Field>
            <Text>Leave the plan empty to ask the planner to inspect the repository and propose tasks. Defaults: 10 attempts, 3 per task, 30 turns, 15 minutes per attempt, 3 hours total. Use the API to configure limits.</Text>
            <Button variant="primary" busy={busy} disabled={!project || !specification || !policy.data?.enabled} onClick={() => void act(async () => {
              const run = await api<Run>('/loop/runs', { method: 'POST', body: { project, specification, worker: { backend, model }, ...(plan.trim() ? { plan: JSON.parse(plan) } : {}) } })
              setSelected(run._id)
            })}>Create draft</Button>
          </Stack>
        </Disclosure>
      </Card>
      <Async r={runs}>{rows => <Card><Stack>{rows.length === 0 && <Text>No Loop runs yet.</Text>}{rows.map(run =>
        <Cluster key={run._id}>
          {/* The project name is unbounded data in a control whose label is
              assumed short: Button is deliberately `shrink-0 whitespace-nowrap`
              (controls keep their intrinsic width, text wraps instead), so a
              long name pushes the button past the viewport — a real 375px
              overflow on `flashnext-control-preparation · 72323e7c`. Truncate
              the NAME and keep the id whole; middle truncation because project
              names discriminate at both ends. */}
          <Button title={`${run.project} · ${run._id}`} onClick={() => { setSelected(run._id); setPlanEdit(''); setPlanVersion(null); setLogAttempt(null) }}>{middleTruncate(run.project, 18)} · {run._id.slice(0, 8)}</Button>
          <Chip>{run.state.replaceAll('_', ' ')}</Chip>
          {run.stop_reason && <Text>{run.stop_reason}</Text>}
        </Cluster>)}</Stack></Card>}</Async>
      {selected && <Async r={detail}>{run => <Stack>
        <Card><Stack>
          <Cluster><Chip>{run.state.replaceAll('_', ' ')}</Chip><span className="break-all text-micro">{run._id}</span></Cluster>
          {run.stop_reason && <p role="status" className="break-words text-micro text-ink-dim">{run.stop_reason}</p>}
          <Cluster>
            {run.state === 'draft' && <>
              <Button busy={busy} onClick={() => void act(() => api(`/loop/runs/${run._id}/plan`, { method: 'POST' }))}>Propose plan</Button>
              <Button busy={busy} disabled={!run.plan} onClick={() => void act(() => api(`/loop/runs/${run._id}/approve`, { method: 'POST', body: { expected_version: run.version } }))}>Approve displayed plan</Button>
            </>}
            {run.state === 'approved' && <Button busy={busy} variant="primary" onClick={() => void act(() => api(`/loop/runs/${run._id}/start`, { method: 'POST' }))}>Start</Button>}
            {run.state === 'paused' && <Button busy={busy} onClick={() => void act(() => api(`/loop/runs/${run._id}/resume`, { method: 'POST' }))}>Resume</Button>}
            {['running', 'planning', 'verifying', 'final_verifying'].includes(run.state) && <Button busy={busy} onClick={() => void act(() => api(`/loop/runs/${run._id}/pause`, { method: 'POST' }))}>Pause after attempt</Button>}
            {['draft', 'approved', 'paused', 'running', 'planning', 'verifying', 'final_verifying'].includes(run.state) && <Button busy={busy} variant="danger" onClick={() => void act(() => api(`/loop/runs/${run._id}/cancel`, { method: 'POST' }))}>Cancel</Button>}
            {['failed', 'running', 'planning', 'verifying', 'final_verifying'].includes(run.state) && <Button busy={busy} onClick={() => void act(() => api(`/loop/runs/${run._id}/recover`, { method: 'POST' }))}>Reconcile after restart</Button>}
          </Cluster>
          <Text>Pause finishes the current attempt and verification. Cancel stops execution and prevents further acceptance. Controls require the existing session admin key.</Text>
          <p className="break-all text-micro">Accepted revision: {run.accepted_revision}</p>
          {run.human_review.length > 0 && <Text>Human acceptance required: {run.human_review.join('; ')}</Text>}
          <Disclosure summary="Specification and proposed plan" defaultOpen={run.state === 'draft'}><Text>{run.specification}</Text><Json value={run.plan} />
            {run.state === 'draft' && <Stack>
              <Button onClick={() => { setPlanEdit(JSON.stringify(run.plan, null, 2)); setPlanVersion(run.version) }}>Edit displayed draft</Button>
              {planVersion !== null && <><Field label="Plan JSON"><Textarea rows={12} value={planEdit} onChange={e => setPlanEdit(e.target.value)} /></Field>
                <Button busy={busy} onClick={() => void act(async () => { await api(`/loop/runs/${run._id}/plan`, { method: 'PUT', body: { plan: JSON.parse(planEdit), expected_version: planVersion } }); setPlanVersion(null) })}>Save draft</Button></>}
            </Stack>}
          </Disclosure>
          <Disclosure summary="Budgets and metrics"><Json value={{ limits: run.limits, usage: run.usage, metrics: run.metrics }} /></Disclosure>
        </Stack></Card>
        {run.tasks.map(t => <Card key={t.id}><Stack>
          <Cluster><span className="font-sans text-prose">{t.id}: {t.description}</span><Chip>{t.state}</Chip><Chip>{t.attempt_count} attempts</Chip></Cluster>
          <Text>{t.acceptance_criteria.join('; ')}</Text>
          <Text>Dependencies: {t.dependencies.join(', ') || 'none'} · Checks: {t.check_ids.join(', ')}</Text>
          {t.handoff && <Text>{t.handoff}</Text>}
          {t.accepted_commit && <p className="break-all text-micro">Checkpoint: {t.accepted_commit}</p>}
        </Stack></Card>)}
        <Card><Stack>{run.attempts.map(a => <Disclosure key={a.id} summary={`${a.task_id} · ${a.outcome}`}>
          <Json value={a} />
          <Button onClick={() => { setLogAttempt(a.id); setLogOffset(0) }}>View logs and diff</Button>
        </Disclosure>)}</Stack></Card>
        {logAttempt && <Card><Stack><Text>Attempt {logAttempt} · evidence records {logOffset + 1}–{logOffset + (logs.data?.length ?? 0)}</Text>
          <Async r={logs}>{rows => <Stack>{rows.map(log => <Disclosure key={log._id} summary={log.kind}><pre className="whitespace-pre-wrap break-words text-micro text-ink-dim">{log.content}</pre></Disclosure>)}</Stack>}</Async>
          <Cluster><Button disabled={logOffset === 0} onClick={() => setLogOffset(Math.max(0, logOffset - 10))}>Previous</Button><Button disabled={(logs.data?.length ?? 0) < 10} onClick={() => setLogOffset(logOffset + 10)}>Next</Button></Cluster>
        </Stack></Card>}
        <Card><Disclosure summary="Lifecycle events and final verification"><Json value={{ events: run.events, final_verification: run.final_evidence }} /></Disclosure></Card>
      </Stack>}</Async>}
    </Stack>
  </AppShell>
}
