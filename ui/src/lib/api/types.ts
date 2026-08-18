/**
 * ARIA - API types (hand-authored)
 *
 * NOT generated: `/openapi.json` declares an empty `{}` 200 schema for
 * `/alerts`, `/infrastructure/model-servers`, `/projects/overview` and
 * `/infrastructure/services` — precisely the payloads the phone screens depend
 * on — so codegen would type them `unknown`. Once those routes grow a
 * `response_model` (BACKLOG §6), switch to openapi-typescript with a drift gate.
 */

/* ------------------------------------------------------------------ alerts */

// Live API emits: info | low | medium | high | critical (verified 2026-08-17).
export type AlertSeverity = 'critical' | 'high' | 'medium' | 'low' | 'info'
export type DecisionValue = 'APPLY' | 'REJECT' | 'STOP' | 'HOLD' | 'IGNORE'

/**
 * Two shapes in the wild: the triage worker writes {root_cause, fix,
 * confidence, evidence}; the steward writes {action, slug, reason}. Render
 * whatever is present rather than assuming one.
 */
export type AlertProposal = {
  root_cause?: string
  fix?: string
  confidence?: number
  evidence?: string | string[]
  action?: string
  slug?: string
  reason?: string
}

export type AlertDecision = {
  by?: string
  at?: string
  value?: DecisionValue
  note?: string
}

export type Alert = {
  id: string
  source?: string
  event_type?: string
  kind?: string
  severity?: AlertSeverity
  needs_human?: boolean
  message?: string
  detail?: string
  dedup_key?: string
  occurrences?: number
  project_slug?: string
  project_path?: string
  acked?: boolean
  delivered_at?: string | null
  created_at?: string
  proposal?: AlertProposal | null
  decision?: AlertDecision | null
}

export type Todo = {
  id: string
  title?: string
  content?: string
  status?: string
  project?: string
  created_at?: string
}

export type SoulProposal = {
  id: string
  stale?: boolean
  created_at?: string
  proposals?: unknown
  reason?: string
}

export type ReviewItem = {
  id: string
  source?: string
  kind?: string
  subject?: string
  detail?: string
  acked?: boolean
  created_at?: string
  updated_at?: string
}

export type ReviewResponse = { items: ReviewItem[]; count?: number }
export type AlertsResponse = { alerts: Alert[]; count?: number }
export type TodosResponse = { tasks: Todo[] }

/* ----------------------------------------------------------------- servers */

export type ModelServer = {
  slug: string
  description?: string
  state?: string
  port?: number
  model_file?: string
  runtime_repo?: string
  runtime_ref?: string
  backend_device?: string
  memory_pool?: string
  also_uses?: string[]
  resident_gib_estimate?: number | null
  measured_resident_gib?: number | null
  weights_present?: boolean
  startable?: boolean
  not_startable_reason?: string
  onbox?: boolean
  endpoint?: string
  consumers_note?: string
  exclusive_with?: string[]
  bound_agents?: string[]
  served_ctx?: number
  slots?: number
  ctx_per_slot?: number
  gtt_used_gib?: number
  gtt_total_gib?: number
  pools?: Record<string, { used_gib?: number; total_gib?: number; spilling?: boolean }>
  parameters?: Array<{ name: string; env?: string; value?: string; description?: string; source?: string }>
}

export type ModelServersResponse = {
  servers: ModelServer[]
  pools?: Record<string, { used_gib?: number; total_gib?: number; spilling?: boolean }>
  host?: string
}

export type ServiceEntry = {
  slug: string
  description?: string
  state?: string
  expected_state?: 'always_up' | 'on_demand'
  kind?: string
  port?: number
  healthy?: boolean
  manageable?: boolean
  needs_review?: boolean
}

export type LlmRoute = {
  mode?: string
  pinned?: string | null
  resolved?: string | null
  reason?: string
}

export type Utilization = {
  servers?: Array<{
    slug: string
    busy_slots?: number | null
    total_slots?: number | null
    utilization?: number | null
    saturated?: boolean | null
    telemetry_hint?: string | null
    tokens_per_second?: number | null
  }>
}

/* ---------------------------------------------------------------- projects */

export type OverviewProject = {
  slug: string
  name: string
  path?: string
  summary?: string
  status?: string
  kind?: string
  activity_status?: string
  attention_score?: number
  last_activity_at?: string
  git?: { branch?: string; dirty?: boolean }
  attention?: {
    blocked_shells?: number
    gate_failed_sessions?: number
    unacked_alerts?: number
    stale_tasks?: number
    working_shells?: number
    running_sessions?: number
  }
}

export type ProjectsOverview = {
  projects: OverviewProject[]
  unacked_alerts_total?: number
  generated_at?: string
}

/* ------------------------------------------------------------------ shells */

export type Shell = {
  name: string
  status?: string
  activity_state?: string
  host?: string
  project?: string
  cwd?: string
  line_count?: number
  last_activity_at?: string
  last_line?: string
  tags?: string[]
}

export type ShellsResponse = { shells: Shell[]; total?: number }

export type ShellOverview = {
  shells?: Array<Shell & { awaiting_input?: boolean }>
  counts?: Record<string, number>
  total?: number
  active?: number
  idle?: number
  stopped?: number
}

export type ShellEvent = {
  line_number: number
  content: string
  ts: string
  kind?: string
}

/* -------------------------------------------------------------------- chat */

export type Agent = {
  id: string
  slug?: string
  name: string
  description?: string
  enabled?: boolean
  model_server?: string | null
  llm?: { backend?: string; model?: string }
  mode_metadata?: { icon?: string }
}

export type Conversation = {
  id: string
  title?: string
  agent_id?: string
  agent_slug?: string
  updated_at?: string
  created_at?: string
  message_count?: number
}

export type Message = {
  id?: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at?: string
}

/* ---------------------------------------------------------------- autonomy */

export type DreamStatus = {
  enabled?: boolean
  running?: boolean
  interval_hours?: number
  active_hours?: { start?: number; end?: number }
  claude_binary?: string
  claude_model?: string
  timeout_seconds?: number
  last_run?: string | null
  last_status?: string | null
  is_active_hours?: boolean
}

export type AwarenessStatus = {
  enabled?: boolean
  running?: boolean
  sensors?: string[]
  poll_interval_seconds?: number
  analysis_interval_minutes?: number
  observation_ttl_hours?: number
  watch_dirs?: string[]
  last_poll?: string | null
  last_analysis?: string | null
}

export type HeartbeatStatus = {
  enabled?: boolean
  running?: boolean
  interval_minutes?: number
  active_hours?: { start?: number; end?: number }
  backend?: string
  model?: string
  heartbeat_file?: string
  heartbeat_file_exists?: boolean
  last_run?: string | null
  last_result?: string | null
  is_active_hours?: boolean
}

export type JournalEntry = {
  id: string
  journal_entry?: string
  connections?: unknown[]
  knowledge_gaps?: unknown[]
  soul_proposals?: unknown[]
  memory_consolidations_proposed?: number
  created_at?: string
  connection_count?: number
  knowledge_gap_count?: number
  soul_proposal_count?: number
}

export type Observation = {
  sensor?: string
  category?: string
  event_type?: string
  summary?: string
  detail?: string | null
  severity?: string
  tags?: string[]
  created_at?: string
}

/** One edit inside a soul proposal: dreams emits {section, current, proposed, reason}. */
export type SoulChange = {
  section?: string
  current?: string
  proposed?: string
  reason?: string
}

/**
 * The full shape `/dreams/soul-proposals` returns. `SoulProposal` above is the
 * subset the Inbox reads; the Autonomy page needs `status` (only `pending` is
 * actionable) and the structured `proposals` list.
 */
export type SoulProposalDetail = SoulProposal & {
  status?: string
  reviewed_at?: string | null
  stale_sections?: string[]
  proposals?: SoulChange[]
}

/* -------------------------------------------------------------- benchmarks */

export type SuiteBench = { id: string; what?: string; category?: string; flags?: string[] }
export type BenchSuite = { name: string; benches: SuiteBench[] }
export type BenchSuitesResponse = { suites: BenchSuite[] }

export type BenchTarget = {
  name: string
  model?: string
  base_url?: string
  vram_gb?: number
  deployment?: string
  cloud?: boolean
  tags?: string[]
}
export type BenchTargetsResponse = { targets: BenchTarget[]; gpu_budget_gb?: number }

export type BenchHealth = { available: boolean; root?: string; binary?: string; gpu_budget_gb?: number }

/**
 * `interrupted` is real: the registry reaper marks a run whose pid died with
 * the API (benchmarks/service.py:176). The old UI's union omitted it, so an
 * interrupted run rendered with `undefined` styling.
 */
export type BenchRunStatus = 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted' | 'unknown'

export type BenchMetric = {
  target?: string
  benchmark?: string
  metric?: string
  value?: number | string
  n?: number | null
}

export type BenchRun = {
  run_id: string
  pid?: number | null
  status: BenchRunStatus
  suites: string[]
  targets: string[]
  limit?: number | null
  started_at: number
  finished_at?: number | null
  returncode?: number | null
  log?: string
  results_dir?: string
  argv?: string[]
  log_tail?: string
  metrics?: BenchMetric[]
  summary?: { run?: string; ok?: boolean; results?: Array<Record<string, unknown>> } | null
}

/** Named BenchRunList to avoid colliding with the operate refit's BenchRunRowsResponse row shape. */
export type BenchRunList = { runs: BenchRun[] }

export type StartBenchRunBody = {
  suites: string[]
  targets: string[]
  run_id?: string
  limit?: number
  allow_coresident?: boolean
  keep_up?: boolean
  force?: boolean
}

/** The 409 detail when a run would disturb a bound model server. */
export type BenchConflictDetail = { error?: string; conflicts?: string[] }

/* ------------------------------------------- supervise: projects + cockpit */
/*
 * Shapes verified against the live API 2026-08-17 (`/projects/overview`,
 * `/projects/aria/cockpit`, `/shells/overview`). Note: `/projects/overview`
 * does NOT yet return `kind`/`charter` (the §4.7 companion API item) — only
 * the per-project cockpit does — so list-level filtering must tolerate
 * `kind === undefined` on every row.
 */

/** The counts behind `attention_score` on both the overview and the cockpit. */
export type AttentionCounts = {
  shells?: number
  blocked_shells?: number
  working_shells?: number
  running_sessions?: number
  gate_failed_sessions?: number
  unacked_alerts?: number
  open_tasks?: number
  stale_tasks?: number
}

/**
 * `/shells/overview` — the fleet digest. The wire-exact `FleetShell` /
 * `FleetOverviewPayload` declarations live in the shells section below; this
 * alias exists so cockpit code names the same shape (shells made optional —
 * the strip must survive a partial payload).
 */
export type FleetOverview = Partial<FleetOverviewPayload>

export type GateRun = { at?: string; passed?: boolean; tail?: string | null }

export type CockpitShell = {
  name: string
  short_name?: string
  status?: string
  activity_state?: 'working' | 'blocked' | 'done' | 'idle' | string
  host?: string
  project_dir?: string
  idle_seconds?: number | null
  awaiting_input?: boolean
  prompt_line?: string | null
  last_line?: string | null
  tags?: string[]
}

export type CockpitSession = {
  id: string
  backend?: string
  model?: string | null
  status?: string
  host?: string | null
  shell_name?: string | null
  workspace?: string | null
  looping?: boolean
  result_summary?: string | null
  gate_runs?: GateRun[]
  created_at?: string
  updated_at?: string
}

export type CockpitTask = {
  id: string
  title?: string
  notes?: string | null
  status?: string
  stale?: boolean
  updated_at?: string | null
}

export type CockpitAlert = {
  id: string
  source?: string
  event_type?: string
  message?: string
  created_at?: string
}

export type ChangedEntry = { content?: string; created_at?: string }

export type LinearItem = {
  id: string
  title?: string
  status?: string
  external_ref?: string | null
  proposed_disposition?: string | null
  updated_at?: string | null
}

export type CockpitBudget = { cost?: number; total_tokens?: number; sessions_priced?: number }

export type CockpitProjectDoc = {
  id?: string
  name?: string
  slug?: string
  summary?: string | null
  status?: string
  kind?: string
  path?: string | null
  next_steps?: string[]
  check_command?: string | null
  activity_status?: string
  last_activity_at?: string | null
  charter?: { purpose?: string; autonomy?: number } & Record<string, unknown>
}

export type ProjectCockpit = {
  project?: CockpitProjectDoc
  attention?: AttentionCounts
  attention_score?: number
  git?: {
    harvested?: { branch?: string | null; last_commit_at?: string | null; last_commit_subject?: string | null } | null
    live?: { branch?: string | null; dirty_files?: number } | null
  }
  shells?: CockpitShell[]
  sessions?: CockpitSession[]
  tasks?: CockpitTask[]
  alerts?: CockpitAlert[]
  changed?: ChangedEntry[]
  linear?: LinearItem[]
  budget?: CockpitBudget
  vault_folder?: string | null
  generated_at?: string
}

/* -------------------------------------------------------------------- know */
/* Appended for the /know/* split (2026-08-17). Shapes verified against the
   live API on this box — /openapi.json types most of these but /projects and
   /tasks are the harvester's loose dicts, so optionality is deliberate. */

export type Memory = {
  id: string
  content: string
  content_type?: string
  importance?: number
  categories?: string[]
  confidence?: number | null
  verified?: boolean
  source?: { type?: string; project?: string; confidence?: number | null }
  access_count?: number
  created_at?: string
}

export type RetrievalSwitch = {
  name?: string
  enabled?: boolean
  reason?: string
  changed_at?: string
  changed_by?: string
}

export type RetrievalCapabilities = {
  embeddings?: RetrievalSwitch
  search?: RetrievalSwitch
  /** The one field that says what a search will actually do. */
  retrieval_mode?: 'hybrid' | 'lexical' | 'fallback' | string
  backfill?: { running?: boolean; pending?: { memories?: number; entities?: number } }
}

export type PlanningTask = {
  id: string
  title?: string
  notes?: string | null
  status?: string
  due_at?: string | null
  project_id?: string | null
  tags?: string[]
  source?: { type?: string; confidence?: number | null }
  created_at?: string
  updated_at?: string
  completed_at?: string | null
}

export type PlanningTasksResponse = { tasks: PlanningTask[] }

export type PlanningProject = {
  id: string
  name: string
  slug?: string
  summary?: string
  status?: string
  kind?: string
  next_steps?: string[]
  recent_activity?: Array<{ at?: string; source?: string; note?: string }>
  activity_status?: string
  last_activity_at?: string | null
}

export type PlanningProjectsResponse = { projects: PlanningProject[] }

export type ResearchRun = {
  id: string
  query: string
  status?: string
  backend?: string
  model?: string
  progress?: {
    current_depth?: number
    max_depth?: number
    queries_completed?: number
    queries_total?: number
    learnings_count?: number
  }
  created_at?: string
  completed_at?: string | null
}

export type BackgroundTask = {
  _id: string
  name?: string
  status?: string
  progress?: number
  error?: string | null
  created_at?: string
  updated_at?: string
}

export type Workflow = {
  _id: string
  name: string
  description?: string
  tags?: string[]
  steps?: Array<{ action?: string; params?: Record<string, unknown>; depends_on?: number[] }>
  created_at?: string
  updated_at?: string
}

export type WorkflowStatus = { workflow?: { name?: string }; runs?: unknown[] }

export type UsageSummary = {
  requests?: number
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  cache_read_tokens?: number
  cache_write_tokens?: number
  cache_hit_rate?: number
}

export type UsageRow = {
  _id?: string | null
  backend?: string
  requests?: number
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  cost?: number
  cache_hit_rate?: number
}

/* -------------------------------------------------- converse (2026-08-17) */

/**
 * Shapes verified against the live API 2026-08-17. The list route strips
 * `messages`; counts live under `stats`, not on the row (the older
 * `Conversation` type above guessed `message_count` — keep using these for
 * the /converse surfaces).
 */
export type ConversationStats = {
  message_count?: number
  total_tokens?: number
  tool_calls?: number
}

export type ConversationListEntry = {
  id: string
  title?: string
  /** ObjectId of the agent the conversation was created with. */
  agent_id?: string
  /** Set by switch-mode; when present it wins over agent_id. */
  active_agent_id?: string | null
  status?: string
  created_at?: string
  updated_at?: string
  tags?: string[]
  pinned?: boolean
  private?: boolean
  stats?: ConversationStats
}

export type ChatToolCall = { id?: string; name?: string; arguments?: unknown }

export type ChatMessage = {
  id?: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  tool_calls?: ChatToolCall[] | null
  model?: string | null
  created_at?: string
}

export type ConversationDetail = ConversationListEntry & {
  summary?: string | null
  llm_config?: { backend?: string; model?: string; temperature?: number }
  messages: ChatMessage[]
}

/** SSE `data:` payload on POST /conversations/{id}/messages (StreamChunk.to_dict). */
export type ChatStreamData = {
  type?: 'text' | 'tool_call' | 'tool_call_delta' | 'done' | 'error'
  content?: string
  tool_call?: { id?: string; name?: string; arguments?: unknown }
  usage?: Record<string, unknown>
  error?: string
}

/* --------------------------------------------------- operate (full shapes) */
/*
 * Appended for the /operate master/detail rebuild (2026-08-17). These mirror
 * the LIVE payloads (verified with curl against :8200 the same day) rather
 * than the looser sketches above: the registry rows carry the fields the
 * detail pages are built around (pool_used/total per row — there is NO
 * top-level `pools` object on /infrastructure/model-servers — plus
 * `parameters` with per-knob `source`, and `can_sleep` for off-box hosts).
 */

export type LaunchParamChoice = { value: string; description?: string }

/**
 * One knob of a deployment's launch configuration. `source` is what makes the
 * value honest: ARIA's own drop-in, a hand-written unit drop-in, the script's
 * default — and only the first is ARIA's to clear on a plain start.
 */
export type LaunchParam = {
  name: string
  env?: string
  label?: string
  kind?: 'int' | 'enum' | 'path' | 'str'
  description?: string
  declared_default?: string | null
  choices?: LaunchParamChoice[]
  value?: string | null
  source?: 'aria_override' | 'unit_dropin' | 'script_default' | 'declared_default' | 'unset'
}

export type ModelServerFull = {
  slug: string
  description?: string
  state?: string
  port?: number | null
  model_file?: string | null
  runtime_repo?: string | null
  runtime_ref?: string | null
  backend_device?: string | null
  resident_gib_estimate?: number | null
  resident_gib_measured?: number | null
  served_ctx?: number | null
  slots?: number | null
  ctx_per_slot?: number | null
  geometry_source?: string | null
  gtt_resident?: boolean
  memory_pool?: string
  also_uses?: string[]
  devices?: string[]
  deployment?: string | null
  pool_used_gib?: number | null
  pool_total_gib?: number | null
  pool_spilling?: boolean
  parameters?: LaunchParam[]
  aria_overrides?: Record<string, string>
  launch_script?: string | null
  systemd_unit?: string | null
  exclusive_with?: string[]
  onbox?: boolean
  startable?: boolean
  not_startable_reason?: string | null
  consumers_note?: string | null
  can_sleep?: boolean
  bound_agents?: string[]
  endpoints?: { local?: string; tailnet?: string }
  gtt_used_gib?: number
  gtt_total_gib?: number
}

export type ModelServersFullResponse = { servers: ModelServerFull[] }

/** GET /infrastructure/llm-route — who answers as "the local model". */
export type LlmRouteFull = {
  pinned?: string | null
  serving?: string | null
  model_id?: string | null
  model_file?: string | null
  backend_device?: string | null
  summary?: string
  reason?: string
  loaded?: Array<{ slug: string; resident_gib?: number | null; backend_device?: string }>
}

/**
 * Live telemetry for one running server. Every numeric field can be null and
 * null means UNKNOWN, never zero — DwarfStar exposes /v1/models only, so its
 * occupancy is unreadable while its token counters still work.
 */
export type UtilServer = {
  slug: string
  reachable?: boolean
  busy_slots?: number | null
  total_slots?: number | null
  free_slots?: number | null
  slot_utilisation?: number | null
  ctx_per_slot?: number | null
  declared_slots?: number | null
  declared_ctx_per_slot?: number | null
  saturated?: boolean | null
  requests_processing?: number | null
  requests_deferred?: number | null
  prompt_tokens_per_second?: number | null
  predicted_tokens_per_second?: number | null
  metrics_available?: boolean
  metrics_hint?: string | null
  telemetry_hint?: string | null
  runtime_family?: string | null
  served_ctx?: number | null
  kv_cache_usage_pct?: number | null
  prefix_cache_hit_rate?: number | null
  prompt_cache_kind?: string | null
  prompt_cache_capacity?: string | null
  prompt_cache_used?: string | null
  bench_decode_tok_s?: number | null
  bench_prefill_tok_s?: number | null
  benchmarked_at?: string | null
  bench_note?: string | null
  resident_gib_measured?: number | null
}

export type UtilizationResponse = { servers?: UtilServer[] }

export type ServiceFull = ServiceEntry & {
  notes?: string | null
  unit?: string | null
  container?: string | null
  compose_file?: string | null
  service_name?: string | null
  depends_on?: string[]
}

export type ServicesResponse = { services: ServiceFull[]; healthy?: number; total?: number }

/* benchmarks — matched to a server by port/slug appearing in the target name */

export type BenchMetricRow = {
  target?: string
  benchmark: string
  metric: string
  value: number
  n?: number | null
}

export type BenchRunRow = {
  run_id: string
  status?: string
  suites?: string[]
  targets?: string[]
  limit?: number | null
  started_at?: number
  finished_at?: number | null
  metrics?: BenchMetricRow[]
}

// NOTE(supervise refit, 2026-08-17): renamed from BenchRunsResponse — it collided
// with the identically-named declaration in the benchmarks section above and
// broke `tsc` for every route. Nothing imported this variant at rename time.
export type BenchRunRowsResponse = { runs?: BenchRunRow[] }

/* ----------------------------------------- shells (supervise refit, §4.5) */
/* Shapes verified against the live API 2026-08-17. The pre-rebuild `Shell` /
   `ShellOverview` types above guessed fields (`project`, `cwd`, `counts`) that
   the API never sends — these mirror the wire exactly. Kept additive so the
   old declarations stay untouched while other refits still compile. */

/** GET /shells + GET /shells/{name} — the shell doc as stored. */
export type ShellInfo = {
  name: string
  short_name?: string
  project_dir?: string
  host?: string
  status?: 'active' | 'idle' | 'stopped' | 'unknown' | string
  created_at?: string
  last_activity_at?: string
  last_output_at?: string | null
  last_input_at?: string | null
  line_count?: number
  tags?: string[]
  metadata?: Record<string, unknown>
}

export type ShellListPayload = { shells: ShellInfo[] }

/** One entry of GET /shells/overview — the enriched fleet digest. */
export type FleetShell = {
  name: string
  short_name?: string
  status?: string
  /** working | blocked | done | idle (see ShellService.fleet_overview). */
  activity_state?: string
  host?: string
  project_dir?: string
  line_count?: number
  last_activity_at?: string
  idle_seconds?: number
  awaiting_input?: boolean
  prompt_line?: string | null
  /** Raw terminal line, up to ~130 chars — render breakable, never nowrap. */
  last_line?: string | null
  tags?: string[]
}

export type FleetOverviewPayload = {
  shells: FleetShell[]
  active_count?: number
  awaiting_count?: number
  blocked_count?: number
  done_count?: number
}

/** GET /shells/{name}/screen — live pane capture (409 when stopped). */
export type ShellScreenPayload = { name: string; lines: number; screen: string }

/** GET /shells/{name}/snapshot — last worker-stored snapshot (survives stop). */
export type ShellSnapshotPayload = {
  shell_name: string
  ts: string
  content: string
  content_hash?: string
  line_count_at_snapshot?: number
}

/** One scrollback event — REST /events entries and `shell_event` SSE frames. */
export type ShellEventWire = {
  shell_name?: string
  ts: string
  line_number: number
  kind?: 'output' | 'input' | 'system' | string
  text_raw?: string
  text_clean?: string
  source?: string
}

export type ShellEventsPayload = { events: ShellEventWire[]; has_more?: boolean }

/** POST /shells/{name}/input response; `screen` present iff wait_ms > 0. */
export type ShellInputResult = { ok: boolean; line_number?: number | null; screen?: string | null }

/** POST /coding/sessions body (routes/coding_sessions.py CodingSessionCreate). */
export type CodingSessionRequest = {
  workspace: string
  prompt: string
  backend?: string
  subagent_profile?: string
  create_worktree?: boolean
  worktree_name?: string
}

/* ------------------------------------------------------------------ pools */

/**
 * The box has TWO independent memory pools plus host RAM, and a model in one
 * does not compete with a model in the other — the single most important
 * operational fact on this machine, and it was not visualised at all: the old
 * meter drew one bar from gtt_used/gtt_total, so the discrete card's 32 GiB was
 * missing entirely.
 *
 * `halo-gtt` is the Strix Halo iGPU drawing from SHARED SYSTEM MEMORY (so the
 * label has to say so); `r9700-vram` is the discrete card's own 32 GiB;
 * `host-ram` is CPU-only servers.
 */
export type MemoryPool = {
  pool: 'halo-gtt' | 'r9700-vram' | 'host-ram' | string
  label: string
  used_gib: number | null
  total_gib: number | null
  free_gib?: number | null
  spilling?: boolean
  source?: string
  /** 'system' pools share the machine's DIMMs; 'device' is a card's own VRAM. */
  backing?: 'system' | 'device' | string
  /** Other pools measuring the same physical memory as this one. */
  overlaps?: string[]
}

export type GpuDevice = {
  card: string
  pci_address?: string
  label: string
  pool: string
  discrete: boolean
  vram_used_gib?: number | null
  vram_total_gib?: number | null
  gtt_used_gib?: number | null
  gtt_total_gib?: number | null
}

/**
 * The composite the flat pool list cannot express: `halo-gtt` and `host-ram`
 * are the same DIMMs, so `igpu_gib + other_gib + available_gib === total_gib`
 * is the only account that adds up.
 */
export type SystemMemory = {
  total_gib: number
  igpu_gib: number
  other_gib: number
  available_gib: number
  igpu_source?: string | null
  source?: string
  note?: string
}

export type DevicesResponse = {
  devices: GpuDevice[]
  pools: MemoryPool[]
  system?: SystemMemory | null
}
