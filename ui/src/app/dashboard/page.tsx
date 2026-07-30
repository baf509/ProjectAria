'use client'

import { startTransition, useEffect, useMemo, useState } from 'react'
import { apiClient } from '@/lib/api-client'
import { ConfirmButton } from '@/components/ConfirmButton'
import type { Agent, Conversation, Memory, PlanningProject, PlanningTask, ResearchRun, Workflow } from '@/types'

type Tab = 'agents' | 'memories' | 'tasks' | 'research' | 'usage' | 'conversations' | 'workflows' | 'settings'

export default function DashboardPage() {
  const [tab, setTab] = useState<Tab>('agents')
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
  const [serverBusy, setServerBusy] = useState<string | null>(null)
  const [serverError, setServerError] = useState<{ slug: string; message: string } | null>(null)
  const [runtimes, setRuntimes] = useState<any[]>([])
  const [pulls, setPulls] = useState<any[]>([])
  const [pullError, setPullError] = useState<string>('')
  const [newPull, setNewPull] = useState({ repo_id: '', filename: '', name: '', runtime: 'mainline-vulkan', port: '' })
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
      apiClient.listModelServers(),
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
      setModels(val(results[8], { servers: [] })?.servers || [])
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

  async function refreshModelServers() {
    try {
      const [serversRes, pullsRes] = await Promise.allSettled([
        apiClient.listModelServers(),
        apiClient.listModelPulls(),
      ])
      startTransition(() => {
        if (serversRes.status === 'fulfilled') setModels(serversRes.value?.servers || [])
        if (pullsRes.status === 'fulfilled') setPulls(pullsRes.value?.pulls || [])
      })
    } catch {
      // panel keeps showing the last known state
    }
  }

  // While a pull is downloading/wiring, poll so progress + the new server appear.
  useEffect(() => {
    const active = pulls.some((p) => !p.stale && (p.status === 'downloading' || p.status === 'wiring'))
    if (!active) return
    const id = setInterval(() => void refreshModelServers(), 5000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pulls])

  useEffect(() => {
    apiClient.listModelRuntimes().then((d) => setRuntimes(d?.runtimes || [])).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function handlePullModel() {
    setPullError('')
    const { repo_id, filename, name, runtime, port } = newPull
    if (!repo_id.trim() || !filename.trim() || !name.trim()) {
      setPullError('Repo, filename, and name are all required.')
      return
    }
    try {
      await apiClient.pullModel({
        repo_id: repo_id.trim(),
        filename: filename.trim(),
        name: name.trim(),
        runtime,
        ...(port.trim() ? { port: Number(port) } : {}),
      })
      setNewPull({ repo_id: '', filename: '', name: '', runtime: 'mainline-vulkan', port: '' })
      setStatusMessage('Pull started — the download runs in the background.')
      await refreshModelServers()
    } catch (e: any) {
      setPullError(e.message || String(e))
    }
  }

  async function handleServerAction(slug: string, action: 'start' | 'stop' | 'sleep', force = false) {
    setServerBusy(slug)
    setServerError(null)
    try {
      let result: any
      if (action === 'start') result = await apiClient.startModelServer(slug, force)
      else if (action === 'stop') result = await apiClient.stopModelServer(slug)
      else result = await apiClient.sleepModelServer(slug)
      setStatusMessage(`${slug}: ${result?.action || action}${result?.detail ? ` — ${result.detail}` : ''}`)
    } catch (e: any) {
      setServerError({ slug, message: e.message || String(e) })
    } finally {
      setServerBusy(null)
      await refreshModelServers()
    }
  }

  async function handleToggleAgent(agent: Agent, force = false) {
    setServerBusy(agent.slug)
    setServerError(null)
    try {
      if (!(agent.enabled ?? true)) {
        // Enabling: bring up the bound model server first, through the
        // RAM/exclusivity gate — a refusal keeps the agent disabled.
        const server = agent.model_server ? models.find((s) => s.slug === agent.model_server) : null
        if (server && server.onbox && !['running', 'paused', 'restarting'].includes(server.state)) {
          await apiClient.startModelServer(server.slug, force)
        }
        await apiClient.updateAgent(agent.id, { enabled: true })
        setStatusMessage(`${agent.name} enabled${server && server.state !== 'running' ? ` — ${server.slug} starting` : ''}.`)
      } else {
        await apiClient.updateAgent(agent.id, { enabled: false })
        const server = agent.model_server ? models.find((s) => s.slug === agent.model_server) : null
        const othersOnServer = agents.some(
          (a) => a.id !== agent.id && (a.enabled ?? true) && a.model_server === agent.model_server,
        )
        setStatusMessage(
          `${agent.name} disabled.${server && server.state === 'running' && !othersOnServer
            ? ` ${server.slug} is still running — stop it from the card if it's no longer needed.` : ''}`,
        )
      }
    } catch (e: any) {
      setServerError({ slug: agent.slug, message: e.message || String(e) })
    } finally {
      setServerBusy(null)
      await Promise.all([refreshDashboard(), refreshModelServers()])
    }
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
      <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 sm:py-10">
        <div className="mb-6 flex flex-wrap items-end justify-between gap-4 sm:mb-8 sm:gap-6">
          <div>
            <p className="mb-2 text-xs uppercase tracking-[0.3em] text-amber-400">Operations Console</p>
            <h1 className="font-serif text-3xl text-stone-50 sm:text-5xl">ARIA Dashboard</h1>
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

        <div className="mb-6 flex flex-wrap gap-2 sm:mb-8 sm:gap-3">
          {(['agents', 'memories', 'tasks', 'research', 'usage', 'conversations', 'workflows', 'settings'] as Tab[]).map((item) => (
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

        {tab === 'agents' && (
          <section className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
            <div className="space-y-4">
            <div className="grid gap-4 md:grid-cols-2">
              {agents.map((agent) => {
                const enabled = agent.enabled ?? true
                const server = agent.model_server ? models.find((s) => s.slug === agent.model_server) : null
                const serverActive = server && ['running', 'paused', 'restarting'].includes(server.state)
                return (
                  <article
                    key={agent.id}
                    className={`rounded-3xl border bg-stone-900 p-5 ${enabled ? 'border-stone-800' : 'border-stone-800/60 opacity-60'}`}
                  >
                    <div className="mb-3 flex items-center justify-between gap-2">
                      <div className="min-w-0 truncate text-lg font-semibold">
                        {agent.mode_metadata?.icon ? `${agent.mode_metadata.icon} ` : ''}{agent.name}
                      </div>
                      <span className={`shrink-0 rounded-full px-3 py-1 text-xs uppercase ${
                        enabled ? 'bg-emerald-950 text-emerald-300' : 'bg-stone-800 text-stone-500'
                      }`}>
                        {enabled ? 'enabled' : 'disabled'}
                      </span>
                    </div>
                    <p className="mb-3 text-sm text-stone-400">{agent.description}</p>
                    <p className="mb-1 text-xs text-stone-500">Model</p>
                    <p className="text-sm text-stone-200">{agent.llm.backend}/{agent.llm.model}</p>
                    <p className="mb-1 mt-3 text-xs text-stone-500">Model server</p>
                    {agent.model_server ? (
                      <div className="flex items-center gap-2 text-sm">
                        <span className="min-w-0 truncate text-stone-200">{agent.model_server}</span>
                        <span className={server?.state === 'running' ? 'text-emerald-400' : 'text-stone-500'}>
                          {server?.state || 'unknown'}
                        </span>
                        {serverActive && server?.onbox && (
                          <ConfirmButton
                            label="Stop"
                            confirmLabel="Confirm stop"
                            disabled={serverBusy !== null}
                            onConfirm={() => void handleServerAction(server.slug, 'stop')}
                            className="rounded-lg border border-rose-800 bg-rose-950/40 px-3 py-2 text-sm text-rose-200 sm:px-2 sm:py-0.5 sm:text-xs hover:bg-rose-950/70 disabled:opacity-40"
                          />
                        )}
                        {server?.can_sleep && (
                          <ConfirmButton
                            label="Sleep"
                            confirmLabel="Confirm sleep"
                            disabled={serverBusy !== null}
                            onConfirm={() => void handleServerAction(server.slug, 'sleep')}
                            className="rounded-lg border border-indigo-800 bg-indigo-950/40 px-3 py-2 text-sm text-indigo-200 sm:px-2 sm:py-0.5 sm:text-xs hover:bg-indigo-950/70 disabled:opacity-40"
                          />
                        )}
                      </div>
                    ) : (
                      <p className="text-sm text-stone-500">
                        not bound — bind one via POST /infrastructure/model-servers/&#123;slug&#125;/bind
                      </p>
                    )}
                    {serverError && serverError.slug === agent.slug && (
                      <div className="mt-2 rounded-xl border border-amber-900/60 bg-amber-950/30 p-2 text-xs text-amber-200">
                        {serverError.message}
                        {serverError.message.includes('force=True') && (
                          <button
                            disabled={serverBusy !== null}
                            onClick={() => void handleToggleAgent(agent, true)}
                            className="ml-2 rounded-lg border border-amber-600 bg-amber-900/50 px-3 py-1.5 text-amber-100 sm:px-2 sm:py-0.5 hover:bg-amber-900/80 disabled:opacity-40"
                          >
                            Force enable
                          </button>
                        )}
                      </div>
                    )}
                    <div className="mt-4 flex gap-2">
                      <button
                        disabled={serverBusy !== null}
                        onClick={() => void handleToggleAgent(agent)}
                        className={`rounded-full border px-4 py-2 text-sm disabled:opacity-40 sm:px-3 sm:py-1 sm:text-xs ${
                          enabled
                            ? 'border-stone-700 text-stone-300 hover:border-stone-500'
                            : 'border-emerald-700 bg-emerald-900/40 text-emerald-200 hover:bg-emerald-900/70'
                        }`}
                      >
                        {serverBusy === agent.slug ? 'Working…' : enabled ? 'Disable' : 'Enable'}
                      </button>
                      <button
                        onClick={() => loadAgentIntoForm(agent)}
                        className="rounded-full border border-stone-700 px-4 py-2 text-sm text-stone-300 sm:px-3 sm:py-1 sm:text-xs hover:border-stone-500"
                      >
                        Edit
                      </button>
                    </div>
                  </article>
                )
              })}
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-xl font-semibold sm:text-2xl">Model Servers</h2>
              <div className="space-y-3">
                {models.map((server) => {
                  const active = ['running', 'paused', 'restarting'].includes(server.state)
                  const canStart = server.onbox && server.startable && !active
                  const canStop = server.onbox && active
                  return (
                    <div key={server.slug} className="rounded-2xl border border-stone-800 bg-stone-950 p-4 text-sm">
                      <div className="mb-1 flex items-center justify-between gap-3 text-stone-100">
                        <span className="min-w-0 truncate">{server.slug}</span>
                        <div className="flex shrink-0 items-center gap-2">
                          <span className={server.state === 'running' ? 'text-emerald-400' : 'text-stone-500'}>
                            {server.state}
                          </span>
                          {canStart && (
                            <button
                              disabled={serverBusy !== null}
                              onClick={() => void handleServerAction(server.slug, 'start')}
                              className="rounded-lg border border-emerald-700 bg-emerald-900/40 px-3 py-2 text-sm text-emerald-200 sm:px-2 sm:py-1 sm:text-xs hover:bg-emerald-900/70 disabled:opacity-40"
                            >
                              {serverBusy === server.slug ? 'Starting…' : 'Start'}
                            </button>
                          )}
                          {canStop && (
                            <ConfirmButton
                              label={serverBusy === server.slug ? 'Stopping…' : 'Stop'}
                              confirmLabel="Confirm stop"
                              disabled={serverBusy !== null}
                              onConfirm={() => void handleServerAction(server.slug, 'stop')}
                              className="rounded-lg border border-rose-800 bg-rose-950/40 px-3 py-2 text-sm text-rose-200 sm:px-2 sm:py-1 sm:text-xs hover:bg-rose-950/70 disabled:opacity-40"
                            />
                          )}
                          {server.can_sleep && (
                            <ConfirmButton
                              label={serverBusy === server.slug ? 'Sleeping…' : 'Sleep'}
                              confirmLabel="Confirm sleep"
                              disabled={serverBusy !== null}
                              onConfirm={() => void handleServerAction(server.slug, 'sleep')}
                              className="rounded-lg border border-indigo-800 bg-indigo-950/40 px-3 py-2 text-sm text-indigo-200 sm:px-2 sm:py-1 sm:text-xs hover:bg-indigo-950/70 disabled:opacity-40"
                            />
                          )}
                        </div>
                      </div>
                      <div className="text-stone-400">
                        {server.backend_device}
                        {server.port ? ` · :${server.port}` : ''}
                        {server.resident_gib_estimate ? ` · ~${server.resident_gib_estimate} GiB` : ''}
                        {server.bound_agents?.length ? ` · agent: ${server.bound_agents.join(', ')}` : ''}
                      </div>
                      {server.endpoints?.tailnet && (
                        <div className="mt-1 flex items-center gap-2">
                          <code className="min-w-0 truncate font-mono text-xs text-stone-500">{server.endpoints.tailnet}</code>
                          <button
                            onClick={() => {
                              void navigator.clipboard?.writeText(server.endpoints.tailnet)
                              setStatusMessage(`Copied ${server.endpoints.tailnet}`)
                            }}
                            className="shrink-0 rounded border border-stone-700 px-2 py-1 text-[10px] uppercase tracking-wide text-stone-400 hover:border-stone-500"
                          >
                            copy
                          </button>
                        </div>
                      )}
                      {!server.startable && server.not_startable_reason && (
                        <div className="mt-1 text-xs text-stone-500">{server.not_startable_reason}</div>
                      )}
                      {serverError && serverError.slug === server.slug && (
                        <div className="mt-2 rounded-xl border border-amber-900/60 bg-amber-950/30 p-2 text-xs text-amber-200">
                          {serverError.message}
                          {serverError.message.includes('force=True') && (
                            <button
                              disabled={serverBusy !== null}
                              onClick={() => void handleServerAction(server.slug, 'start', true)}
                              className="ml-2 rounded-lg border border-amber-600 bg-amber-900/50 px-3 py-1.5 text-amber-100 sm:px-2 sm:py-0.5 hover:bg-amber-900/80 disabled:opacity-40"
                            >
                              Force start
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
              {models.length > 0 && models[0].gtt_used_gib != null && (
                <p className="mt-3 text-xs text-stone-500">
                  GPU unified memory (GTT): {models[0].gtt_used_gib} / {models[0].gtt_total_gib} GiB used
                </p>
              )}

              <h3 className="mb-2 mt-6 text-xs uppercase tracking-[0.2em] text-stone-400">
                Pull new model from Hugging Face
              </h3>
              <div className="space-y-2">
                <input
                  value={newPull.repo_id}
                  onChange={(e) => setNewPull({ ...newPull, repo_id: e.target.value })}
                  placeholder="Repo — e.g. unsloth/Qwen3.6-27B-MTP-GGUF"
                  className="w-full rounded-xl border border-stone-700 bg-stone-950 px-3 py-2 text-sm text-stone-100 placeholder-stone-600 focus:border-amber-400 focus:outline-none"
                />
                <input
                  value={newPull.filename}
                  onChange={(e) => setNewPull({ ...newPull, filename: e.target.value })}
                  placeholder="GGUF filename — e.g. Qwen3.6-27B-MTP-Q4_K_M.gguf"
                  className="w-full rounded-xl border border-stone-700 bg-stone-950 px-3 py-2 text-sm text-stone-100 placeholder-stone-600 focus:border-amber-400 focus:outline-none"
                />
                <div className="flex gap-2">
                  <input
                    value={newPull.name}
                    onChange={(e) => setNewPull({ ...newPull, name: e.target.value })}
                    placeholder="Server name (slug)"
                    className="flex-1 rounded-xl border border-stone-700 bg-stone-950 px-3 py-2 text-sm text-stone-100 placeholder-stone-600 focus:border-amber-400 focus:outline-none"
                  />
                  <input
                    value={newPull.port}
                    onChange={(e) => setNewPull({ ...newPull, port: e.target.value })}
                    placeholder="Port (auto)"
                    className="w-28 rounded-xl border border-stone-700 bg-stone-950 px-3 py-2 text-sm text-stone-100 placeholder-stone-600 focus:border-amber-400 focus:outline-none"
                  />
                </div>
                <div className="flex gap-2">
                  <select
                    value={newPull.runtime}
                    onChange={(e) => setNewPull({ ...newPull, runtime: e.target.value })}
                    className="flex-1 rounded-xl border border-stone-700 bg-stone-950 px-3 py-2 text-sm text-stone-100 focus:border-amber-400 focus:outline-none"
                  >
                    {(runtimes.length ? runtimes : [{ slug: 'mainline-vulkan', description: 'mainline llama.cpp, Vulkan GPU' }]).map((r) => (
                      <option key={r.slug} value={r.slug}>
                        {r.slug} — {r.description}
                      </option>
                    ))}
                  </select>
                  <button
                    onClick={() => void handlePullModel()}
                    className="rounded-xl border border-amber-400 bg-amber-400 px-4 py-2 text-sm font-medium text-stone-950 hover:bg-amber-300"
                  >
                    Pull
                  </button>
                </div>
                {pullError && (
                  <div className="rounded-xl border border-rose-900/60 bg-rose-950/30 p-2 text-xs text-rose-200">
                    {pullError}
                  </div>
                )}
                {pulls.filter((p) => !(p.status === 'completed' && p.stale === false)).slice(0, 5).map((p) => (
                  <div key={p.job_id} className="rounded-xl border border-stone-800 bg-stone-950 p-2 text-xs">
                    <div className="flex items-center justify-between text-stone-200">
                      <span>{p.slug} ← {p.repo_id}</span>
                      <span className={
                        p.status === 'completed' ? 'text-emerald-400'
                        : p.status === 'failed' || p.stale ? 'text-rose-400'
                        : 'text-amber-300'
                      }>
                        {p.stale && p.status !== 'completed' && p.status !== 'failed' ? 'stale (api restarted)' : p.status}
                      </span>
                    </div>
                    {p.error && <div className="mt-1 text-rose-300">{p.error}</div>}
                    {!p.error && p.status === 'downloading' && p.log_tail && (
                      <pre className="mt-1 max-h-16 overflow-hidden whitespace-pre-wrap text-stone-500">{p.log_tail.slice(-300)}</pre>
                    )}
                  </div>
                ))}
              </div>
            </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <div className="mb-4 flex items-center justify-between gap-4">
                <h2 className="text-2xl font-semibold">{editingAgentId ? 'Edit Agent' : 'Agent Creation Wizard'}</h2>
                {editingAgentId ? (
                  <button
                    onClick={resetModeForm}
                    className="rounded-full border border-stone-700 px-4 py-2 text-sm text-stone-300 sm:px-3 sm:py-1 sm:text-xs hover:border-stone-500"
                  >
                    New Agent
                  </button>
                ) : null}
              </div>
              <div className="space-y-3">
                <input
                  value={newMode.name}
                  onChange={(e) => setNewMode((prev) => ({ ...prev, name: e.target.value }))}
                  placeholder="Agent name"
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
                        setStatusMessage(`Updated agent ${newMode.name}.`)
                      } else {
                        await apiClient.createAgent(payload)
                        setStatusMessage(`Created agent ${newMode.name}.`)
                      }
                      await refreshDashboard()
                      resetModeForm()
                    }}
                    className="rounded-full bg-amber-400 px-4 py-2 text-sm font-medium text-stone-950"
                  >
                    {editingAgentId ? 'Save Agent' : 'Create Agent'}
                  </button>
                  {editingAgentId ? (
                    <>
                      <button
                        onClick={resetModeForm}
                        className="rounded-full border border-stone-700 px-4 py-2 text-sm text-stone-300 hover:border-stone-500"
                      >
                        Cancel
                      </button>
                      <ConfirmButton
                        label="Delete agent"
                        confirmLabel="Confirm delete"
                        onConfirm={async () => {
                          await apiClient.deleteAgent(editingAgentId)
                          setStatusMessage(`Deleted agent ${newMode.name}.`)
                          resetModeForm()
                          await refreshDashboard()
                        }}
                        className="rounded-full border border-red-900 px-4 py-2 text-sm text-red-400 sm:px-3 sm:py-1 sm:text-xs hover:border-red-700 hover:bg-red-950"
                      />
                    </>
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
                    <ConfirmButton
                      label="Delete"
                      confirmLabel="Confirm delete"
                      onConfirm={async () => {
                        await apiClient.deleteMemory(memory.id)
                        setStatusMessage('Memory deleted.')
                        await refreshDashboard()
                      }}
                      className="rounded-full border border-red-900 px-4 py-2 text-sm text-red-400 sm:px-3 sm:py-1 sm:text-xs hover:border-red-700 hover:bg-red-950"
                    />
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
                              className="rounded-lg border border-emerald-700 bg-emerald-900/40 px-3 py-2 text-sm text-emerald-200 sm:px-2 sm:py-1 sm:text-xs hover:bg-emerald-900/70"
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
                              className="rounded-lg border border-emerald-700 bg-emerald-900/40 px-3 py-2 text-sm text-emerald-200 sm:px-2 sm:py-1 sm:text-xs hover:bg-emerald-900/70"
                            >
                              Done
                            </button>
                            <ConfirmButton
                              label="Delete"
                              confirmLabel="Confirm delete"
                              onConfirm={() => void handleTodoAction(t.id, 'delete')}
                              className="rounded-lg border border-stone-700 bg-stone-800 px-2 py-1 text-xs text-stone-500 hover:text-rose-300"
                            />
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
              <h2 className="mb-4 text-xl font-semibold sm:text-2xl">Research Runs</h2>
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
              <h2 className="mb-4 text-xl font-semibold sm:text-2xl">Background Tasks</h2>
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
              <h2 className="mb-4 text-xl font-semibold sm:text-2xl">Summary</h2>
              <div className="space-y-3 text-sm text-stone-300">
                <div>Requests: {usage?.requests ?? 0}</div>
                <div>Input tokens: {usage?.input_tokens ?? 0}</div>
                <div>Output tokens: {usage?.output_tokens ?? 0}</div>
                <div>Total tokens: {usage?.total_tokens ?? 0}</div>
              </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-xl font-semibold sm:text-2xl">By Agent</h2>
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
              <h2 className="mb-4 text-xl font-semibold sm:text-2xl">By Model</h2>
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
                        className="rounded-full border border-stone-700 px-4 py-2 text-sm text-stone-300 sm:px-3 sm:py-1 sm:text-xs hover:border-stone-500"
                      >
                        Export Markdown
                      </button>
                      <ConfirmButton
                        label="Delete"
                        confirmLabel="Confirm delete"
                        onConfirm={async () => {
                          await apiClient.deleteConversation(conversation.id)
                          setStatusMessage(`Deleted conversation.`)
                          await refreshDashboard()
                        }}
                        className="rounded-full border border-red-900 px-4 py-2 text-sm text-red-400 sm:px-3 sm:py-1 sm:text-xs hover:border-red-700 hover:bg-red-950"
                      />
                    </div>
                  </article>
                ))}
              </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-xl font-semibold sm:text-2xl">Export Preview</h2>
              <pre className="max-h-[70vh] overflow-auto rounded-2xl bg-stone-950 p-4 text-xs text-stone-300">
                {selectedConversationExport || 'Select a conversation to preview exported markdown.'}
              </pre>
            </div>
          </section>
        )}

        {tab === 'workflows' && (
          <section className="grid gap-4 xl:grid-cols-[1fr_0.9fr]">
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-xl font-semibold sm:text-2xl">Workflow Library</h2>
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
                          className="rounded-full border border-stone-700 px-4 py-2 text-sm text-stone-300 sm:px-3 sm:py-1 sm:text-xs hover:border-stone-500"
                        >
                          Status
                        </button>
                        <button
                          onClick={async () => {
                            await apiClient.runWorkflow(workflow._id, true)
                            setStatusMessage(`Started dry run for ${workflow.name}.`)
                            setTasks(await apiClient.listTasks())
                          }}
                          className="rounded-full border border-stone-700 px-4 py-2 text-sm text-stone-300 sm:px-3 sm:py-1 sm:text-xs hover:border-stone-500"
                        >
                          Dry Run
                        </button>
                        <button
                          onClick={async () => {
                            await apiClient.runWorkflow(workflow._id)
                            setStatusMessage(`Started workflow ${workflow.name}.`)
                            setTasks(await apiClient.listTasks())
                          }}
                          className="rounded-full border border-stone-700 px-4 py-2 text-sm text-stone-300 sm:px-3 sm:py-1 sm:text-xs hover:border-stone-500"
                        >
                          Run
                        </button>
                        <ConfirmButton
                          label="Delete"
                          confirmLabel="Confirm delete"
                          onConfirm={async () => {
                            await apiClient.deleteWorkflow(workflow._id)
                            setStatusMessage(`Deleted workflow ${workflow.name}.`)
                            await refreshDashboard()
                          }}
                          className="rounded-full border border-red-900 px-4 py-2 text-sm text-red-400 sm:px-3 sm:py-1 sm:text-xs hover:border-red-700 hover:bg-red-950"
                        />
                      </div>
                    </div>
                    <p className="text-sm text-stone-400">{workflow.description}</p>
                    <p className="mt-2 text-xs text-stone-500">{workflow.steps.length} steps</p>
                  </article>
                ))}
              </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-xl font-semibold sm:text-2xl">Create Workflow</h2>
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
              <h2 className="mb-4 text-xl font-semibold sm:text-2xl">Runtime Notes</h2>
              <div className="space-y-3 text-sm text-stone-400">
                <p>Settings editing is still partial. This panel currently exposes runtime state for models and tasks.</p>
                <p>API-key auth and configurable CORS are now backend-configurable via environment variables.</p>
                <p>Use the chat UI for live mode switching, or the CLI for scripting and automation.</p>
              </div>
            </div>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-6">
              <h2 className="mb-4 text-xl font-semibold sm:text-2xl">Cutover Readiness</h2>
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
