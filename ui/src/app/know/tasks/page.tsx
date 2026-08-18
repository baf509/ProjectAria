'use client'

/**
 * ARIA - Know: tasks (todos + planning projects)
 *
 * One segment, two resources — the old dashboard loaded these alongside twelve
 * unrelated endpoints, so the todo list stayed blank until the 8.8s
 * model-servers call returned. Proposed (ambient-extracted) todos keep their
 * review affordance; active ones can be completed or deleted; both columns
 * keep the quick-add inputs the old page had.
 */
import { useState } from 'react'
import { useResource, useAction } from '@/lib/swr'
import {
  K,
  acceptTodo,
  dismissTodo,
  createTodo,
  completeTodo,
  deleteTodo,
  createPlanningProject,
} from '@/lib/api/endpoints'
import type { PlanningTask, PlanningTasksResponse, PlanningProject, PlanningProjectsResponse } from '@/lib/api/types'
import { Card, Chip, Text, EmptyState, Code } from '@/components/ui/primitives'
import { Button, ConfirmButton, Disclosure, Input, Toasts } from '@/components/ui/controls'
import { Async } from '@/components/ui/Async'
import { Stack, Cluster, Columns } from '@/components/layout'
import { relativeTime } from '@/lib/time'
import { useKnowStats } from '@/features/know/knowStatus'
import { useToasts } from '@/features/know/useToasts'

function TodoRow({ todo, onDone, onError }: { todo: PlanningTask; onDone: (t: string) => void; onError: (t: string) => void }) {
  const run = useAction()
  const [busy, setBusy] = useState<string | null>(null)
  const proposed = todo.status === 'proposed'

  async function act(label: string, fn: () => Promise<unknown>, done: string) {
    setBusy(label)
    const ok = await run(fn, { invalidate: ['/todos'], onError: (e) => onError(e.message) })
    setBusy(null)
    if (ok !== undefined) onDone(done)
  }

  return (
    <li className="border-b border-line py-2 last:border-b-0">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 font-sans text-prose text-ink">{todo.title}</span>
        {todo.due_at && <Chip tone="accent">due {todo.due_at.slice(0, 10)}</Chip>}
        {proposed ? (
          <>
            <Button busy={busy === 'accept'} onClick={() => act('accept', () => acceptTodo(todo.id), 'Accepted')}>
              Accept
            </Button>
            <Button busy={busy === 'dismiss'} onClick={() => act('dismiss', () => dismissTodo(todo.id), 'Dismissed')}>
              Dismiss
            </Button>
          </>
        ) : (
          <>
            <Button busy={busy === 'done'} onClick={() => act('done', () => completeTodo(todo.id), 'Marked done')}>
              Done
            </Button>
            <ConfirmButton
              label="Delete"
              onConfirm={() => act('delete', () => deleteTodo(todo.id), 'Deleted')}
            />
          </>
        )}
      </div>
      {todo.notes && <Text clamp={2} className="mt-1">{todo.notes}</Text>}
      {proposed && todo.source?.type === 'conversation' && (
        <p className="m-0 mt-1 text-micro text-ink-faint">
          from conversation · confidence {(todo.source.confidence ?? 0).toFixed(2)}
        </p>
      )}
    </li>
  )
}

function ProjectRow({ project }: { project: PlanningProject }) {
  const steps = project.next_steps ?? []
  const activity = (project.recent_activity ?? []).slice(-3)
  return (
    <li className="cv-auto border-b border-line py-2 last:border-b-0">
      <Disclosure
        summary={
          <span className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="min-w-0 font-sans text-prose text-ink">{project.name}</span>
            <Chip tone={project.status === 'active' ? 'ok' : project.status === 'paused' ? 'accent' : 'neutral'}>
              {project.status || 'unknown'}
            </Chip>
            <span className="ml-auto shrink-0 text-micro text-ink-faint">{relativeTime(project.last_activity_at)}</span>
          </span>
        }
      >
        <Stack gap="sm">
          <Code>{project.slug}</Code>
          {project.summary && <Text>{project.summary}</Text>}
          {steps.length > 0 && (
            <div>
              <p className="m-0 text-micro uppercase tracking-[0.1em] text-ink-faint">Next steps</p>
              <ul className="m-0 mt-1 list-none p-0">
                {steps.map((s, i) => (
                  <li key={i} className="font-sans text-prose text-ink-dim">
                    • {s}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {activity.length > 0 && (
            <div>
              <p className="m-0 text-micro uppercase tracking-[0.1em] text-ink-faint">Recent activity</p>
              <ul className="m-0 mt-1 list-none p-0">
                {activity.map((a, i) => (
                  <li key={i} className="text-micro text-ink-dim">
                    <span className="text-ink-faint">{relativeTime(a.at)}</span> {a.note}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Stack>
      </Disclosure>
    </li>
  )
}

export default function TasksPage() {
  const toasts = useToasts()
  const run = useAction()
  const todos = useResource<PlanningTasksResponse>(K.todosPlanning, { tier: 'lazy' })
  const projects = useResource<PlanningProjectsResponse>(K.planningProjects, { tier: 'lazy' })
  const [newTodo, setNewTodo] = useState('')
  const [newProject, setNewProject] = useState('')
  const [busy, setBusy] = useState<string | null>(null)

  const all = todos.data?.tasks ?? []
  const proposed = all.filter((t) => t.status === 'proposed')
  const active = all.filter((t) => t.status === 'active')
  // Active projects first — the harvester registers scratch dirs too, and they
  // used to bury the real ones.
  const projectList = [...(projects.data?.projects ?? [])].sort(
    (a, b) => (a.status === 'active' ? 0 : 1) - (b.status === 'active' ? 0 : 1)
  )

  useKnowStats([
    { label: 'PROPOSED', value: proposed.length, tone: proposed.length > 0 ? 'warn' : 'default' },
    { label: 'ACTIVE', value: active.length },
    { label: 'PROJECTS', value: projectList.length },
  ])

  async function addTodo() {
    const title = newTodo.trim()
    if (!title) return
    setBusy('todo')
    const ok = await run(() => createTodo(title), { invalidate: ['/todos'], onError: (e) => toasts.warn(e.message) })
    setBusy(null)
    if (ok !== undefined) {
      setNewTodo('')
      toasts.ok('Todo added')
    }
  }

  async function addProject() {
    const name = newProject.trim()
    if (!name) return
    setBusy('project')
    const ok = await run(() => createPlanningProject(name), {
      invalidate: ['/projects'],
      onError: (e) => toasts.warn(e.message),
    })
    setBusy(null)
    if (ok !== undefined) {
      setNewProject('')
      toasts.ok('Project added')
    }
  }

  return (
    <>
      <Columns lg={2}>
        <Card title={`Todos · ${all.length}`} hint={`${proposed.length} proposed · ${active.length} active`}>
          <Stack gap="sm">
            <Cluster nowrap={false} className="flex-nowrap">
              <Input
                value={newTodo}
                onChange={(e) => setNewTodo(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void addTodo()
                }}
                placeholder="Add a todo…"
                aria-label="New todo"
                className="min-w-0 flex-1 coarse:text-title"
              />
              <Button busy={busy === 'todo'} onClick={() => void addTodo()}>
                Add
              </Button>
            </Cluster>
            <Async r={todos} skeletonRows={4} isEmpty={(d) => (d.tasks?.length ?? 0) === 0} empty="Nothing on the list. Add one above.">
              {() => (
                <Stack gap="sm">
                  {proposed.length > 0 && (
                    <div>
                      <p className="m-0 text-micro uppercase tracking-[0.1em] text-accent">Proposed (review)</p>
                      <ul className="m-0 list-none p-0">
                        {proposed.map((t) => (
                          <TodoRow key={t.id} todo={t} onDone={toasts.ok} onError={toasts.warn} />
                        ))}
                      </ul>
                    </div>
                  )}
                  <div>
                    <p className="m-0 text-micro uppercase tracking-[0.1em] text-live">Active</p>
                    {active.length === 0 ? (
                      <EmptyState>Nothing active. Accept a proposal or add one above.</EmptyState>
                    ) : (
                      <ul className="m-0 list-none p-0">
                        {active.map((t) => (
                          <TodoRow key={t.id} todo={t} onDone={toasts.ok} onError={toasts.warn} />
                        ))}
                      </ul>
                    )}
                  </div>
                </Stack>
              )}
            </Async>
          </Stack>
        </Card>

        <Card title={`Projects · ${projectList.length}`} hint="planning registry">
          <Stack gap="sm">
            <Cluster className="flex-nowrap">
              <Input
                value={newProject}
                onChange={(e) => setNewProject(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void addProject()
                }}
                placeholder="New project name…"
                aria-label="New project"
                className="min-w-0 flex-1 coarse:text-title"
              />
              <Button busy={busy === 'project'} onClick={() => void addProject()}>
                Add
              </Button>
            </Cluster>
            <Async
              r={projects}
              skeletonRows={4}
              isEmpty={(d) => (d.projects?.length ?? 0) === 0}
              empty="No projects yet. Create one so ARIA can attach todos to it."
            >
              {() => (
                <ul className="m-0 list-none p-0">
                  {projectList.map((p) => (
                    <ProjectRow key={p.id} project={p} />
                  ))}
                </ul>
              )}
            </Async>
          </Stack>
        </Card>
      </Columns>
      <Toasts toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </>
  )
}
