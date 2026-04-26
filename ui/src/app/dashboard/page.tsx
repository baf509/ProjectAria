'use client'

import { startTransition, useEffect, useMemo, useState } from 'react'
import { apiClient } from '@/lib/api-client'
import type { Agent, Conversation, Memory, PlanningProject, PlanningTask, ResearchRun, Workflow } from '@/types'

type Tab = 'modes' | 'memories' | 'tasks' | 'research' | 'usage' | 'conversations' | 'workflows' | 'settings'

export default function DashboardPage() {
  const [tab, setTab] = useState<Tab>('modes')
  const [statusMessage, setStatusMessage] = useState<string>('')
  const [agents, setAgents] = useState<Agent[]>([])
  const [editingAgentId, setEditingAgentId] = useState<string | null>(null)
  const [memories, setMemories] = useState<Memory[]>([])
  const [memoryQuery, setMemoryQuery] = useState('')
  const [researchRuns, setResearchRuns] = useState<ResearchRun[]>([])
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [conversationQuery, setConversationQuery] = useState('')
  const [selectedConversationExport, setSelectedConversationExport] = useState<string>('')
  const [usage, setUsage] = useState<any>(null)
  const [usageByAgent, setUsageByAgent] = useState<any[]>([])
  const [usageByModel, setUsageByModel] = useState<any[]>([])
  const [tasks, setTasks] = useState<any[]>([])
  const [todos, setTodos] = useState<PlanningTask[]>([])
  const [planningProjects, setPlanningProjects] = useState<PlanningProject[]>([])
  const [newTodoTitle, setNewTodoTitle] = useState('')
  const [newProjectName, setNewProjectName] = useState('')
  const [models, setModels] = useState<any[]>([])
  const [workflows, setWorkflows] = useState<Workflow[]>([])
  const [workflowStatus, setWorkflowStatus] = useState<any | null>(null)
  const [auditOverview, setAuditOverview] = useState<any | null>(null)
  const [cutover, setCutover] = useState<any | null>(null)
  const [newMode, setNewMode] = useState({
    name: '',
    slug: '',
    description: '',
    system_prompt: '',
    mode_category: 'chat',
    backend: 'llamacpp',
    model: 'default',
    temperature: '0.7',
    icon: '',
    keywords: '',
    greeting: '',
    context_instructions: '',
    keyboard_shortcut: '',
  })
  const [newWorkflow, setNewWorkflow] = useState({
    name: '',
    description: '',
    stepsJson: '[{"action":"notify","params":{"detail":"hello","event_type":"info"}}]',
  })

  async function refreshDashboard() {
    const results = await Promise.allSettled([
      apiClient.listAgents(),
      apiClient.listMemories(50),
      apiClient.listResearchRuns(),
      apiClient.listConversations(50),
      apiClient.usageSummary(),
      apiClient.usageByAgent(),
      apiClient.usageByModel(),
      apiClient.listTasks(),
      apiClient.listInfrastructureModels(),
      apiClient.listWorkflows(),
      apiClient.auditOverview(),
      apiClient.cutoverStatus(),
      apiClient.listTodos('proposed,active'),
      apiClient.listPlanningProjects(),
    ])

    const val = <T,>(r: PromiseSettledResult<T>, fallback: T): T =>
      r.status === 'fulfilled' ? r.value : fallback

    startTransition(() => {
      setAgents(val(results[0], []))
      setMemories(val(results[1], []))
      setResearchRuns(val(results[2], []))
      setConversations(val(results[3], []))
      setUsage(val(results[4], null))
      setUsageByAgent(val(results[5], []))
      setUsageByModel(val(results[6], []))
      setTasks(val(results[7], []))
      setModels(val(results[8], { models: [] })?.models || [])
      setWorkflows(val(results[9], []))
      setAuditOverview(val(results[10], null))
      setCutover(val(results[11], null))
      setTodos(val(results[12], []))
      setPlanningProjects(val(results[13], []))
    })
  }

  async function refreshPlanning() {
    const [todosResult, projectsResult] = await Promise.allSettled([
      apiClient.listTodos('proposed,active'),
      apiClient.listPlanningProjects(),
    ])
    startTransition(() => {
      if (todosResult.status === 'fulfilled') setTodos(todosResult.value)
      if (projectsResult.status === 'fulfilled') setPlanningProjects(projectsResult.value)
    })
  }

  async function handleCreateTodo() {
    const title = newTodoTitle.trim()
    if (!title) return
    try {
      await apiClient.createTodo({ title })
      setNewTodoTitle('')
      setStatusMessage('Todo added.')
      await refreshPlanning()
    } catch (e: any) {
      setStatusMessage(`Add failed: ${e.message || e}`)
    }
  }

  async function handleTodoAction(taskId: string, action: 'accept' | 'done' | 'dismiss' | 'delete') {
    try {
      if (action === 'accept') await apiClient.acceptTodo(taskId)
      else if (action === 'done') await apiClient.completeTodo(taskId)
      else if (action === 'dismiss') await apiClient.dismissTodo(taskId)
      else await apiClient.deleteTodo(taskId)
      await refreshPlanning()
    } catch (e: any) {
      setStatusMessage(`Action failed: ${e.message || e}`)
    }
  }

  async function handleCreateProject() {
    const name = newProjectName.trim()
    if (!name) return
    try {
      await apiClient.createPlanningProject({ name })
      setNewProjectName('')
      setStatusMessage('Project added.')
      await refreshPlanning()
    } catch (e: any) {
      setStatusMessage(`Add failed: ${e.message || e}`)
    }
  }

  function resetModeForm() {
    setEditingAgentId(null)
    setNewMode({
      name: '',
      slug: '',
      description: '',
      system_prompt: '',
      mode_category: 'chat',
      backend: 'llamacpp',
      model: 'default',
      temperature: '0.7',
      icon: '',
      keywords: '',
      greeting: '',
      context_instructions: '',
      keyboard_shortcut: '',
    })
  }

  function loadAgentIntoForm(agent: Agent) {
    setEditingAgentId(agent.id)
    setNewMode({
      name: agent.name,
      slug: agent.slug,
      description: agent.description,
      system_prompt: agent.system_prompt,
      mode_category: agent.mode_category || 'chat',
      backend: agent.llm.backend,
      model: agent.llm.model,
      temperature: String(agent.llm.temperature ?? 0.7),
      icon: agent.mode_metadata?.icon || '',
      keywords: (agent.mode_metadata?.keywords || []).join(', '),
      greeting: agent.greeting || '',
      context_instructions: agent.context_instructions || '',
      keyboard_shortcut: agent.mode_metadata?.keyboard_shortcut || '',
    })
  }

  useEffect(() => {
    void refreshDashboard()
  }, [])

  const filteredMemories = useMemo(() => {
    if (!memoryQuery.trim()) return memories
    return memories.filter((memory) =>
      `${memory.content} ${memory.content_type} ${memory.categories.join(' ')}`.toLowerCase().includes(memoryQuery.toLowerCase()),
    )
  }, [memories, memoryQuery])

  const filteredConversations = useMemo(() => {
    if (!conversationQuery.trim()) return conversations
    return conversations.filter((conversation) =>
      `${conversation.title} ${conversation.summary || ''}`.toLowerCase().includes(conversationQuery.toLowerCase()),
    )
  }, [conversations, conversationQuery])

  return (
    <main className="min-h-screen bg-stone-950 text-stone-100">
      <div className="mx-auto max-w-7xl px-6 py-10">
        <div className="mb-8 flex items-end justify-between gap-6">
          <div>
            <p className="mb-2 text-xs uppercase tracking-[0.3em] text-amber-400">Operations Console</p>
            <h1 className="font-serif text-5xl text-stone-50">ARIA Dashboard</h1>
            <p className="mt-3 max-w-2xl text-sm text-stone-400">
              Modes, memory, research, task health, and runtime settings in one place.
            </p>
          </div>
        </div>
        {statusMessage ? (
          <div className="mb-6 rounded-2xl border border-emerald-800 bg-emerald-950/40 px-4 py-3 text-sm text-emerald-200">
            {statusMessage}
          </div>
        ) : null}

        <div className="mb-8 flex flex-wrap gap-3">
          {(['modes', 'memories', 'tasks', 'research', 'usage', 'conversations', 'workflows', 'settings'] as Tab[]).map((item) => (
            <button
              key={item}
              onClick={() => setTab(item)}
              className={`rounded-full border px-4 py-2 text-sm capitalize transition ${
                tab === item
                  ? 'border-amber-400 bg-amber-400 text-stone-950'
                  : 'border-stone-700 bg-stone-900 text-stone-300 hover:border-stone-500'
              }`}
            >
              {item}
            </button>
          ))}
          <a
            href="/dashboard/shells"
            className="rounded-full border border-stone-700 bg-stone-900 px-4 py-2 text-sm capitalize text-stone-300 transition hover:border-stone-500"
          >
            shells
          </a>
        </div>

        {tab === 'modes' && (
          <section className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
            <div className="grid gap-4 md:grid-cols-2">
              {agents.map((agent) => (
                <article key={agent.id} className="rounded-3xl border border-stone-800 bg-stone-900 p-5">
                  <div className="mb-3 flex items-center justify-between">
                    <div className="text-lg font-semibold">
                      {agent.mode_metadata?.icon ? `${agent.mode_metadata.icon} ` : ''}{agent.name}
                    </div>
                    <div className="rounded-full bg-stone-800 px-3 py-1 text-xs uppercase text-stone-300">
                      {agent.mode_category || 'chat'}
                    </div>
                  </div>
                  <p className="mb-3 text-sm text-stone-400">{agent.description}</p>
                  <p className="mb-2 text-xs text-stone-500">Model</p>
                  <p className="text-sm text-stone-200">{agent.llm.backend}/{agent.llm.model}</p>
                  {agent.mode_metadata?.keywords?.length ? (
                    <div className="mt-4 flex flex-wrap gap-2">
                      {agent.mode_metadata.keywords.map((keyword) => (
                        <span key={keyword} className="rounded-full bg-stone-800 px-2 py-1 text-xs text-stone-300">
                          {keyword}
                        </span>
                      ))}
                    </div>
                  ) : null}
                  <div className="mt-4 flex gap-2">
                    <button
                      onClick={() => loadAgentIntoForm(agent)}
                      className="rounded-full border border-stone-700 px-3 py-1 text-xs text-stone-300 hover:border-stone-500"
                    >
                      Edit
                    </button>
                    <button
                      onClick={async () => {
                        if (!confirm(`Delete mode "${agent.name}"?`)) return
                        await apiClient.deleteAgent(agent.id)
                        setStatusMessage(`Deleted mode ${agent.name}.`)
                        await refreshDashboard()
                      }}
                      className="rounded-full border border-red-900 px-3 py-1 text-xs text-red-400 hover:border-red-700 hover:bg-red-950"
                    >
                      Delete
                    </button>
                  </div>
                </article>
              ))}
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <div className="mb-4 flex items-center justify-between gap-4">
                <h2 className="text-2xl font-semibold">{editingAgentId ? 'Edit Mode' : 'Mode Creation Wizard'}</h2>
                {editingAgentId ? (
                  <button
                    onClick={resetModeForm}
                    className="rounded-full border border-stone-700 px-3 py-1 text-xs text-stone-300 hover:border-stone-500"
                  >
                    New Mode
                  </button>
                ) : null}
              </div>
              <div className="space-y-3">
                <input
                  value={newMode.name}
                  onChange={(e) => setNewMode((prev) => ({ ...prev, name: e.target.value }))}
                  placeholder="Mode name"
                  className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                />
                <input
                  value={newMode.slug}
                  onChange={(e) => setNewMode((prev) => ({ ...prev, slug: e.target.value }))}
                  placeholder="Slug"
                  className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                />
                <textarea
                  value={newMode.description}
                  onChange={(e) => setNewMode((prev) => ({ ...prev, description: e.target.value }))}
                  placeholder="Description"
                  rows={2}
                  className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                />
                <textarea
                  value={newMode.system_prompt}
                  onChange={(e) => setNewMode((prev) => ({ ...prev, system_prompt: e.target.value }))}
                  placeholder="System prompt"
                  rows={5}
                  className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                />
                <input
                  value={newMode.greeting}
                  onChange={(e) => setNewMode((prev) => ({ ...prev, greeting: e.target.value }))}
                  placeholder="Greeting"
                  className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                />
                <textarea
                  value={newMode.context_instructions}
                  onChange={(e) => setNewMode((prev) => ({ ...prev, context_instructions: e.target.value }))}
                  placeholder="Context instructions"
                  rows={3}
                  className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                />
                <div className="grid gap-3 md:grid-cols-2">
                  <input
                    value={newMode.mode_category}
                    onChange={(e) => setNewMode((prev) => ({ ...prev, mode_category: e.target.value }))}
                    placeholder="Category"
                    className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                  />
                  <input
                    value={newMode.icon}
                    onChange={(e) => setNewMode((prev) => ({ ...prev, icon: e.target.value }))}
                    placeholder="Icon"
                    className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                  />
                  <input
                    value={newMode.backend}
                    onChange={(e) => setNewMode((prev) => ({ ...prev, backend: e.target.value }))}
                    placeholder="LLM backend"
                    className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                  />
                  <input
                    value={newMode.model}
                    onChange={(e) => setNewMode((prev) => ({ ...prev, model: e.target.value }))}
                    placeholder="Model"
                    className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                  />
                  <input
                    value={newMode.temperature}
                    onChange={(e) => setNewMode((prev) => ({ ...prev, temperature: e.target.value }))}
                    placeholder="Temperature"
                    className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                  />
                  <input
                    value={newMode.keywords}
                    onChange={(e) => setNewMode((prev) => ({ ...prev, keywords: e.target.value }))}
                    placeholder="Keywords comma-separated"
                    className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                  />
                  <input
                    value={newMode.keyboard_shortcut}
                    onChange={(e) => setNewMode((prev) => ({ ...prev, keyboard_shortcut: e.target.value }))}
                    placeholder="Shortcut"
                    className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                  />
                </div>
                <div className="flex flex-wrap gap-3">
                  <button
                    onClick={async () => {
                      const payload = {
                        name: newMode.name,
                        slug: newMode.slug,
                        description: newMode.description,
                        system_prompt: newMode.system_prompt,
                        mode_category: newMode.mode_category,
                        greeting: newMode.greeting || null,
                        context_instructions: newMode.context_instructions || null,
                        mode_metadata: {
                          icon: newMode.icon || null,
                          keyboard_shortcut: newMode.keyboard_shortcut || null,
                          keywords: newMode.keywords
                            .split(',')
                            .map((item) => item.trim())
                            .filter(Boolean),
                        },
                        llm: {
                          backend: newMode.backend,
                          model: newMode.model,
                          temperature: Number(newMode.temperature || 0.7),
                          max_tokens: 4096,
                        },
                      }

                      if (editingAgentId) {
                        await apiClient.updateAgent(editingAgentId, payload)
                        setStatusMessage(`Updated mode ${newMode.name}.`)
                      } else {
                        await apiClient.createAgent(payload)
                        setStatusMessage(`Created mode ${newMode.name}.`)
                      }
                      await refreshDashboard()
                      resetModeForm()
                    }}
                    className="rounded-full bg-amber-400 px-4 py-2 text-sm font-medium text-stone-950"
                  >
                    {editingAgentId ? 'Save Mode' : 'Create Mode'}
                  </button>
                  {editingAgentId ? (
                    <button
                      onClick={resetModeForm}
                      className="rounded-full border border-stone-700 px-4 py-2 text-sm text-stone-300 hover:border-stone-500"
                    >
                      Cancel
                    </button>
                  ) : null}
                </div>
              </div>
            </div>
          </section>
        )}

        {tab === 'memories' && (
          <section className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
            <div className="mb-4 flex items-center justify-between gap-4">
              <h2 className="text-2xl font-semibold">Memory Browser</h2>
              <input
                value={memoryQuery}
                onChange={(e) => setMemoryQuery(e.target.value)}
                placeholder="Search memories"
                className="w-full max-w-md rounded-full border border-stone-700 bg-stone-950 px-4 py-2 text-sm text-stone-100 outline-none"
              />
            </div>
            <div className="space-y-3">
              {filteredMemories.map((memory) => (
                <article key={memory.id} className="rounded-2xl border border-stone-800 bg-stone-950 p-4">
                  <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-wide text-stone-500">
                    <span>{memory.content_type}</span>
                    <span>confidence {memory.confidence ?? 'n/a'}</span>
                  </div>
                  <p className="text-sm text-stone-100">{memory.content}</p>
                  <div className="mt-3 flex items-center justify-between">
                    <div className="flex flex-wrap gap-2">
                      {memory.categories.map((category) => (
                        <span key={category} className="rounded-full bg-stone-800 px-2 py-1 text-xs text-stone-300">
                          {category}
                        </span>
                      ))}
                    </div>
                    <button
                      onClick={async () => {
                        if (!confirm('Delete this memory?')) return
                        await apiClient.deleteMemory(memory.id)
                        setStatusMessage('Memory deleted.')
                        await refreshDashboard()
                      }}
                      className="rounded-full border border-red-900 px-3 py-1 text-xs text-red-400 hover:border-red-700 hover:bg-red-950"
                    >
                      Delete
                    </button>
                  </div>
                </article>
              ))}
            </div>
          </section>
        )}

        {tab === 'tasks' && (
          <section className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
            {/* Todos column */}
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <div className="mb-4 flex items-center justify-between gap-4">
                <h2 className="text-2xl font-semibold">Todos</h2>
                <span className="text-xs text-stone-500">
                  {todos.filter((t) => t.status === 'proposed').length} proposed ·{' '}
                  {todos.filter((t) => t.status === 'active').length} active
                </span>
              </div>
              <div className="mb-6 flex gap-2">
                <input
                  value={newTodoTitle}
                  onChange={(e) => setNewTodoTitle(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') void handleCreateTodo() }}
                  placeholder="Add a todo and press Enter…"
                  className="flex-1 rounded-xl border border-stone-700 bg-stone-950 px-3 py-2 text-sm text-stone-100 placeholder-stone-600 focus:border-amber-400 focus:outline-none"
                />
                <button
                  onClick={() => void handleCreateTodo()}
                  className="rounded-xl border border-amber-400 bg-amber-400 px-4 py-2 text-sm font-medium text-stone-950 hover:bg-amber-300"
                >
                  Add
                </button>
              </div>

              {/* Proposed (ambient) — review queue */}
              {todos.filter((t) => t.status === 'proposed').length > 0 && (
                <div className="mb-6">
                  <h3 className="mb-2 text-xs uppercase tracking-[0.2em] text-fuchsia-400">
                    Proposed (review)
                  </h3>
                  <div className="space-y-2">
                    {todos.filter((t) => t.status === 'proposed').map((t) => (
                      <article key={t.id} className="rounded-xl border border-fuchsia-900/50 bg-fuchsia-950/20 p-3">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <p className="text-sm text-stone-100">{t.title}</p>
                            {t.notes && <p className="mt-1 text-xs text-stone-400">{t.notes}</p>}
                            <p className="mt-1 text-[11px] text-stone-500">
                              {t.source.type === 'conversation'
                                ? `from conversation · confidence ${(t.source.confidence ?? 0).toFixed(2)}`
                                : `from ${t.source.type}`}
                            </p>
                          </div>
                          <div className="flex shrink-0 gap-1">
                            <button
                              onClick={() => void handleTodoAction(t.id, 'accept')}
                              className="rounded-lg border border-emerald-700 bg-emerald-900/40 px-2 py-1 text-xs text-emerald-200 hover:bg-emerald-900/70"
                            >
                              Accept
                            </button>
                            <button
                              onClick={() => void handleTodoAction(t.id, 'dismiss')}
                              className="rounded-lg border border-stone-700 bg-stone-800 px-2 py-1 text-xs text-stone-400 hover:bg-stone-700"
                            >
                              Dismiss
                            </button>
                          </div>
                        </div>
                      </article>
                    ))}
                  </div>
                </div>
              )}

              {/* Active */}
              <div>
                <h3 className="mb-2 text-xs uppercase tracking-[0.2em] text-emerald-400">Active</h3>
                {todos.filter((t) => t.status === 'active').length === 0 ? (
                  <p className="text-sm text-stone-500">Nothing on the list. Add one above or accept a proposal.</p>
                ) : (
                  <div className="space-y-2">
                    {todos.filter((t) => t.status === 'active').map((t) => (
                      <article key={t.id} className="rounded-xl border border-stone-800 bg-stone-950 p-3">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <p className="text-sm text-stone-100">{t.title}</p>
                            {t.notes && <p className="mt-1 text-xs text-stone-400">{t.notes}</p>}
                            {t.due_at && (
                              <p className="mt-1 text-[11px] text-amber-400">due {t.due_at.slice(0, 10)}</p>
                            )}
                          </div>
                          <div className="flex shrink-0 gap-1">
                            <button
                              onClick={() => void handleTodoAction(t.id, 'done')}
                              className="rounded-lg border border-emerald-700 bg-emerald-900/40 px-2 py-1 text-xs text-emerald-200 hover:bg-emerald-900/70"
                            >
                              Done
                            </button>
                            <button
                              onClick={() => void handleTodoAction(t.id, 'delete')}
                              className="rounded-lg border border-stone-700 bg-stone-800 px-2 py-1 text-xs text-stone-500 hover:text-rose-300"
                            >
                              Delete
                            </button>
                          </div>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* Projects column */}
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <div className="mb-4 flex items-center justify-between gap-4">
                <h2 className="text-2xl font-semibold">Projects</h2>
                <span className="text-xs text-stone-500">{planningProjects.length} active</span>
              </div>
              <div className="mb-6 flex gap-2">
                <input
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') void handleCreateProject() }}
                  placeholder="New project name…"
                  className="flex-1 rounded-xl border border-stone-700 bg-stone-950 px-3 py-2 text-sm text-stone-100 placeholder-stone-600 focus:border-amber-400 focus:outline-none"
                />
                <button
                  onClick={() => void handleCreateProject()}
                  className="rounded-xl border border-stone-700 bg-stone-800 px-4 py-2 text-sm text-stone-200 hover:border-amber-400 hover:text-amber-300"
                >
                  Add
                </button>
              </div>

              {planningProjects.length === 0 ? (
                <p className="text-sm text-stone-500">
                  No projects yet. Create one above so ARIA can attach todos and capture status updates against it.
                </p>
              ) : (
                <div className="space-y-3">
                  {planningProjects.map((p) => (
                    <article key={p.id} className="rounded-xl border border-stone-800 bg-stone-950 p-4">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <h3 className="text-sm font-semibold text-stone-100">{p.name}</h3>
                          <p className="text-[11px] text-stone-500">{p.slug}</p>
                        </div>
                        <span className={`rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-widest ${
                          p.status === 'active'
                            ? 'border-emerald-800 bg-emerald-950/40 text-emerald-300'
                            : p.status === 'paused'
                            ? 'border-amber-800 bg-amber-950/40 text-amber-300'
                            : 'border-stone-700 bg-stone-800 text-stone-400'
                        }`}>
                          {p.status}
                        </span>
                      </div>
                      {p.summary && <p className="mt-2 text-xs text-stone-400">{p.summary}</p>}
                      {p.next_steps.length > 0 && (
                        <div className="mt-3">
                          <p className="text-[10px] uppercase tracking-widest text-stone-500">Next steps</p>
                          <ul className="mt-1 space-y-0.5">
                            {p.next_steps.map((step, i) => (
                              <li key={i} className="text-xs text-stone-300">• {step}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {p.recent_activity.length > 0 && (
                        <div className="mt-3">
                          <p className="text-[10px] uppercase tracking-widest text-stone-500">Recent activity</p>
                          <ul className="mt-1 space-y-0.5">
                            {p.recent_activity.slice(-3).map((a, i) => (
                              <li key={i} className="text-[11px] text-stone-400">
                                <span className="text-stone-600">{a.at.slice(0, 16).replace('T', ' ')}</span> {a.note}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </article>
                  ))}
                </div>
              )}
            </div>
          </section>
        )}

        {tab === 'research' && (
          <section className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-2xl font-semibold">Research Runs</h2>
              <div className="space-y-3">
                {researchRuns.map((run) => (
                  <article key={run.id} className="rounded-2xl border border-stone-800 bg-stone-950 p-4">
                    <div className="mb-2 flex items-center justify-between gap-4">
                      <h3 className="font-medium text-stone-100">{run.query}</h3>
                      <span className="rounded-full bg-stone-800 px-2 py-1 text-xs uppercase text-stone-300">
                        {run.status}
                      </span>
                    </div>
                    <p className="text-xs text-stone-500">
                      Depth {run.progress.current_depth}/{run.progress.max_depth} ·
                      Queries {run.progress.queries_completed}/{run.progress.queries_total} ·
                      Learnings {run.progress.learnings_count}
                    </p>
                  </article>
                ))}
              </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-2xl font-semibold">Background Tasks</h2>
              <div className="space-y-3">
                {tasks.slice(0, 10).map((task) => (
                  <article key={task._id} className="rounded-2xl border border-stone-800 bg-stone-950 p-4">
                    <div className="mb-2 flex items-center justify-between">
                      <div className="text-sm text-stone-100">{task.name}</div>
                      <div className="text-xs uppercase text-stone-400">{task.status}</div>
                    </div>
                    <div className="h-2 overflow-hidden rounded-full bg-stone-800">
                      <div className="h-full bg-amber-400" style={{ width: `${task.progress || 0}%` }} />
                    </div>
                  </article>
                ))}
              </div>
            </div>
          </section>
        )}

        {tab === 'usage' && (
          <section className="grid gap-4 xl:grid-cols-[0.8fr_1.1fr_1.1fr]">
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-2xl font-semibold">Summary</h2>
              <div className="space-y-3 text-sm text-stone-300">
                <div>Requests: {usage?.requests ?? 0}</div>
                <div>Input tokens: {usage?.input_tokens ?? 0}</div>
                <div>Output tokens: {usage?.output_tokens ?? 0}</div>
                <div>Total tokens: {usage?.total_tokens ?? 0}</div>
              </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-2xl font-semibold">By Agent</h2>
              <div className="space-y-3">
                {usageByAgent.map((row) => (
                  <div key={row._id || 'unknown'} className="rounded-2xl border border-stone-800 bg-stone-950 p-4 text-sm">
                    <div className="mb-1 text-stone-100">{row._id || 'unknown'}</div>
                    <div className="text-stone-400">{row.total_tokens} tokens · {row.requests} requests</div>
                  </div>
                ))}
              </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-2xl font-semibold">By Model</h2>
              <div className="space-y-3">
                {usageByModel.map((row) => (
                  <div key={row._id || 'unknown'} className="rounded-2xl border border-stone-800 bg-stone-950 p-4 text-sm">
                    <div className="mb-1 text-stone-100">{row._id || 'unknown'}</div>
                    <div className="text-stone-400">{row.total_tokens} tokens · {row.requests} requests</div>
                  </div>
                ))}
              </div>
            </div>
          </section>
        )}

        {tab === 'conversations' && (
          <section className="grid gap-4 xl:grid-cols-[1fr_0.8fr]">
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <div className="mb-4 flex items-center justify-between gap-4">
                <h2 className="text-2xl font-semibold">Conversation Management</h2>
                <input
                  value={conversationQuery}
                  onChange={(e) => setConversationQuery(e.target.value)}
                  placeholder="Search conversations"
                  className="w-full max-w-md rounded-full border border-stone-700 bg-stone-950 px-4 py-2 text-sm text-stone-100 outline-none"
                />
              </div>
              <div className="space-y-3">
                {filteredConversations.map((conversation) => (
                  <article key={conversation.id} className="rounded-2xl border border-stone-800 bg-stone-950 p-4">
                    <div className="mb-2 flex items-center justify-between gap-4">
                      <div className="font-medium text-stone-100">{conversation.title}</div>
                      <div className="text-xs uppercase text-stone-500">{conversation.status}</div>
                    </div>
                    <p className="text-sm text-stone-400">{conversation.summary || 'No summary yet.'}</p>
                    <div className="mt-3 flex gap-2">
                      <button
                        onClick={async () => {
                          const exported = await apiClient.exportConversation(conversation.id, 'markdown')
                          setSelectedConversationExport(exported.content || '')
                        }}
                        className="rounded-full border border-stone-700 px-3 py-1 text-xs text-stone-300 hover:border-stone-500"
                      >
                        Export Markdown
                      </button>
                      <button
                        onClick={async () => {
                          if (!confirm(`Delete conversation "${conversation.title}"?`)) return
                          await apiClient.deleteConversation(conversation.id)
                          setStatusMessage(`Deleted conversation.`)
                          await refreshDashboard()
                        }}
                        className="rounded-full border border-red-900 px-3 py-1 text-xs text-red-400 hover:border-red-700 hover:bg-red-950"
                      >
                        Delete
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-2xl font-semibold">Export Preview</h2>
              <pre className="max-h-[70vh] overflow-auto rounded-2xl bg-stone-950 p-4 text-xs text-stone-300">
                {selectedConversationExport || 'Select a conversation to preview exported markdown.'}
              </pre>
            </div>
          </section>
        )}

        {tab === 'workflows' && (
          <section className="grid gap-4 xl:grid-cols-[1fr_0.9fr]">
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-2xl font-semibold">Workflow Library</h2>
              <div className="space-y-3">
                {workflows.map((workflow) => (
                  <article key={workflow._id} className="rounded-2xl border border-stone-800 bg-stone-950 p-4">
                    <div className="mb-2 flex items-center justify-between">
                      <div className="font-medium text-stone-100">{workflow.name}</div>
                      <div className="flex gap-2">
                        <button
                          onClick={async () => {
                            const status = await apiClient.workflowStatus(workflow._id)
                            setWorkflowStatus(status)
                          }}
                          className="rounded-full border border-stone-700 px-3 py-1 text-xs text-stone-300 hover:border-stone-500"
                        >
                          Status
                        </button>
                        <button
                          onClick={async () => {
                            await apiClient.runWorkflow(workflow._id, true)
                            setStatusMessage(`Started dry run for ${workflow.name}.`)
                            setTasks(await apiClient.listTasks())
                          }}
                          className="rounded-full border border-stone-700 px-3 py-1 text-xs text-stone-300 hover:border-stone-500"
                        >
                          Dry Run
                        </button>
                        <button
                          onClick={async () => {
                            await apiClient.runWorkflow(workflow._id)
                            setStatusMessage(`Started workflow ${workflow.name}.`)
                            setTasks(await apiClient.listTasks())
                          }}
                          className="rounded-full border border-stone-700 px-3 py-1 text-xs text-stone-300 hover:border-stone-500"
                        >
                          Run
                        </button>
                        <button
                          onClick={async () => {
                            if (!confirm(`Delete workflow "${workflow.name}"?`)) return
                            await apiClient.deleteWorkflow(workflow._id)
                            setStatusMessage(`Deleted workflow ${workflow.name}.`)
                            await refreshDashboard()
                          }}
                          className="rounded-full border border-red-900 px-3 py-1 text-xs text-red-400 hover:border-red-700 hover:bg-red-950"
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                    <p className="text-sm text-stone-400">{workflow.description}</p>
                    <p className="mt-2 text-xs text-stone-500">{workflow.steps.length} steps</p>
                  </article>
                ))}
              </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-2xl font-semibold">Create Workflow</h2>
              <div className="space-y-3">
                <input
                  value={newWorkflow.name}
                  onChange={(e) => setNewWorkflow((prev) => ({ ...prev, name: e.target.value }))}
                  placeholder="Workflow name"
                  className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                />
                <textarea
                  value={newWorkflow.description}
                  onChange={(e) => setNewWorkflow((prev) => ({ ...prev, description: e.target.value }))}
                  placeholder="Description"
                  className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 text-sm text-stone-100 outline-none"
                  rows={3}
                />
                <textarea
                  value={newWorkflow.stepsJson}
                  onChange={(e) => setNewWorkflow((prev) => ({ ...prev, stepsJson: e.target.value }))}
                  className="w-full rounded-2xl border border-stone-700 bg-stone-950 px-4 py-3 font-mono text-xs text-stone-100 outline-none"
                  rows={10}
                />
                <p className="text-xs text-stone-500">
                  Steps support <code className="rounded bg-stone-800 px-1 py-0.5">{'{{steps.0.response}}'}</code>, dependency arrays,
                  and condition gates like <code className="rounded bg-stone-800 px-1 py-0.5">{'{"action":"condition","params":{"value":"{{steps.0.status}}","equals":"success"}}'}</code>.
                </p>
                <button
                  onClick={async () => {
                    let steps
                    try {
                      steps = JSON.parse(newWorkflow.stepsJson)
                    } catch {
                      setStatusMessage('Invalid JSON in steps.')
                      return
                    }
                    await apiClient.createWorkflow({
                      name: newWorkflow.name,
                      description: newWorkflow.description,
                      steps,
                    })
                    setStatusMessage(`Created workflow ${newWorkflow.name}.`)
                    setWorkflows(await apiClient.listWorkflows())
                  }}
                  className="rounded-full bg-amber-400 px-4 py-2 text-sm font-medium text-stone-950"
                >
                  Create Workflow
                </button>
                {workflowStatus ? (
                  <div className="rounded-2xl border border-stone-800 bg-stone-950 p-4 text-xs text-stone-300">
                    <div className="mb-2 text-sm text-stone-100">{workflowStatus.workflow?.name || 'Workflow status'}</div>
                    <pre className="max-h-64 overflow-auto whitespace-pre-wrap">
                      {JSON.stringify(workflowStatus.runs?.slice(0, 3) || [], null, 2)}
                    </pre>
                  </div>
                ) : null}
              </div>
            </div>
          </section>
        )}

        {tab === 'settings' && (
          <section className="grid gap-4 md:grid-cols-2">
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-2xl font-semibold">Infrastructure Models</h2>
              <div className="space-y-3">
                {models.map((model) => (
                  <div key={model.name} className="rounded-2xl border border-stone-800 bg-stone-950 p-4 text-sm">
                    <div className="mb-1 text-stone-100">{model.name}</div>
                    <div className="text-stone-400">{model.backend} · {model.active ? 'active' : 'available'}</div>
                  </div>
                ))}
              </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-2xl font-semibold">Runtime Notes</h2>
              <div className="space-y-3 text-sm text-stone-400">
                <p>Settings editing is still partial. This panel currently exposes runtime state for models and tasks.</p>
                <p>API-key auth and configurable CORS are now backend-configurable via environment variables.</p>
                <p>Use the chat UI for live mode switching, or the CLI for scripting and automation.</p>
              </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-2xl font-semibold">Cutover Readiness</h2>
              <div className="space-y-3 text-sm text-stone-300">
                <div className="rounded-2xl bg-stone-950 p-4">
                  <div className="mb-2 text-stone-100">Ready: {cutover?.ready ? 'yes' : 'not yet'}</div>
                  {(cutover?.checklist || []).map((item: any) => (
                    <div key={item.key} className="flex items-center justify-between border-t border-stone-800 py-2 first:border-t-0">
                      <span>{item.label}</span>
                      <span className="text-xs uppercase text-stone-400">{item.status}</span>
                    </div>
                  ))}
                </div>
                <div className="rounded-2xl bg-stone-950 p-4">
                  <div className="mb-2 text-stone-100">Audit Summary</div>
                  <div className="text-stone-400">
                    {(auditOverview?.summary?.events || []).length} grouped event buckets in the last {auditOverview?.summary?.hours || 24}h
                  </div>
                </div>
              </div>
            </div>
          </section>
        )}
      </div>
    </main>
  )
}
