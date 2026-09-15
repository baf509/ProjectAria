# ARIA and Hermes tool audit — 2026-09-15

ARIA has substantial existing capability. The most useful next work is repairing browsing, reconciling stale tool and model settings, and exposing the existing document/research operations. Additional external integrations should follow actual workflows.

## Scope and evidence

Inspected the active API release `20260914T095033Z-c67127d-c4c1df95047e`, active MCP release `20260914T101143Z-08d03b3fbc0a`, and the running Hermes profile `/Users/ben/Services/data/hermes-home`. The canonical ARIA checkout contains unrelated pending changes; it was not deployed. No model selection, service restart, integration enablement, message delivery or benchmark launch was performed.

Live inventory: **127 outward-facing ARIA MCP tools**, plus **39 tools in ARIA’s internal execution router (16 built-in, 23 Playwright)**. These are different interfaces with overlapping functions, not 166 independent capabilities. Hermes discovers 131 ARIA entries: 127 tools and four MCP resource/prompt utilities. Its gateway readiness record identifies PID 23717, Signal, connected, 131 registered/selected entries, and no missing required tools.

Fresh Hermes discovery in gateway/interactive contexts resolved **153 Signal tools** (131 ARIA + 22 native) and **152 CLI tools** (131 ARIA + 21 native). The initial model-facing set contains 20 definitions, including `tool_search`, `tool_describe` and `tool_call`; further schemas are exposed on demand. The running gateway’s last `model_visible` observation was null, so this audit does not claim that a particular existing conversation’s model request contained a verified catalog.

API readiness and all 14 service-health entries passed their configured checks. Some optional services are deliberately off; a green service summary is not proof that every tool works. Ten representative MCP calls completed successfully: readiness, contract, fleet, nodes, guard, loop policy, benchmark catalog, temperatures, research status and memory search. Mutating tools were inspected, not exhaustively executed.

## What is configured

| Area | Current capability and observed status |
|---|---|
| Models and GPUs | Model inventory, routing, lifecycle, Red selection, GPU inventory, temperature and inference tracing tools. PARO remains the Hermes default; vision and routine cron use the 3090. Temperature and model-readiness paths are reachable. |
| Coding | Sessions, watched terminals, output/diffs, pause/stop/resume, deadlines, mechanical review, checkpoints, rollback, merge gates, and durable loops. Fleet/node/guard/loop reads passed. No code job or mutation was started. |
| Projects and tasks | ARIA projects, charters, tasks, alerts, workflow controls and steward operations are exposed. Linear ticket creation is advertised but the Linear integration is not configured. |
| Memory and knowledge graph | Store/read/update/search and graph tools are exposed. A PARO recall query succeeded using fallback retrieval. Embeddings and mongot search are intentionally disabled. |
| Research and web | Hermes Tavily search and extraction passed live public-page checks. ARIA `browse_page` passed. Research status/report reads exist; research start is API-only. |
| Interactive browsing | Both browser paths need repair: ARIA Playwright lacks its selected browser; Hermes selects a Linux executable on macOS. |
| Files and documents | Hermes file tools can extract common document formats. A synthetic text PDF was read correctly. ARIA has DOCX/XLSX/PDF generation code and dependencies, but its tool is blocked and absent from the outward MCP bridge. |
| Vision and audio | Same-day Hermes vision test passed on `NInfer-3090-Qwen3.8-27B`. Hermes uses Edge TTS and local Whisper configuration; audio was not generated/transcribed in this audit. ARIA’s separate TTS/STT services are stopped on demand. |
| Scheduling and monitoring | Hermes `cronjob_manage` is available in gateway/interactive contexts; ARIA alerts, awareness and steward tools are exposed. Routine cron defaults to the 3090. No scheduled job was manually triggered. |
| Optional creative/productivity tools | Image generation has no available provider in the fresh catalog. Spotify is listed for CLI but unavailable. Kanban tools are unavailable; ARIA already provides project/task tools. Linear MCP is explicitly disabled. No GitHub, email or calendar MCP connection is configured in ARIA/Hermes. |

## Repairs I would prioritize

1. **Repair both browser installations.** ARIA’s Playwright MCP connects and advertises 23 tools, but `browser_tabs(action="list")` fails: `Browser "chrome-for-testing" is not installed`. Hermes’s `_find_cli()` chooses `/Users/ben/Services/data/hermes-home/bin/uvx`; `file` identifies it and the neighboring `uv` as Linux x86-64 ELF executables. An offline version probe fails with `exec format error`. A native `/opt/homebrew/bin/uvx` exists, but the managed copy takes precedence. Configure a native, pinned Hermes browser CLI and a compatible browser for ARIA, then verify an isolated page navigation and screenshot through each actual tool path. Search/extract availability should remain separate from browser availability.

2. **Reconcile registered tools, allowlists and agent records.** `web_fetch` is registered, but the allowlist and all five internal agent records still refer to `web`; a live execution attempt is rejected before fetching. Internal records also name unregistered `deep_think` and, for two records, `claude_agent`. Decide which should be renamed, restored or removed, and validate all enabled names against registration at startup. The generic router also denies session mutations while the outward MCP uses dedicated session APIs; that difference is not by itself a defect. Keep the intentional sensitive-tool boundary for generic filesystem/shell access. These internal agent records are separate from Pi’s own runtime tools.

3. **Complete model-default bookkeeping.** Live `/health/config` reports the enabled steward pinned to `Red-Qwen3.8-27B-MXFP4`. The benchmark catalog contains `red-radiance` (old MXFP4), `flash-next` (CUDA/Halo candidate), `red-flash-next`, and `halogen`; it has no PARO or 3090 target. Add explicit current targets and retain old variants as named comparison options. Route the steward according to its intended role—3090 for routine background work, PARO for Red reasoning work—instead of leaving an accidental old-model pin. Do not change the existing 3090 cron/vision choices.

4. **Add functional capability checks.** Current service health and MCP connectivity did not detect the browser failures or stale names. A small read-only canary set should check discovery, actual execution, intended backend identity and output—not merely HTTP 200. Keep registered/enabled/reachable/tested states distinct. Include browser navigation, search/extract, PDF reading, vision, memory fallback and model-default drift. Do not silently start disabled optional services as part of health checks.

## Useful additions after those repairs

| Priority | Addition | Why it fits this setup | Configuration work |
|---|---|---|---|
| High | Document export through ARIA MCP | Turn research and scanner output into PDF, Word or Excel files without asking the model to assemble shell scripts. | Existing `generate_document` code and python-docx/openpyxl/reportlab are present. Add a typed MCP operation and explicit policy entry. Constrain filenames/output paths before exposing it; the existing generator directly joins the provided filename. Verify each output format and make the artifact easy to retrieve. |
| High | Start and follow a research run | Hermes can read research status/results but cannot directly start one through the ARIA bridge. | Wrap the existing `POST /api/v1/research` with topic, explicit backend and bounded budget; return a durable run ID for the existing read tools and Obsidian publishing. Exercise the research backend before marking this ready. |
| High | Current benchmark profiles and result lookup | Recent PARO/250 W measurements live in detailed engineering reports outside the old benchmark target list. | Register PARO and 3090, add repeatable performance/tool-use/coding profiles with power caps and runtime revisions recorded, and link results to existing benchmark tools. The catalog currently reports 14 suites available; this is discovery evidence, not execution of all optional benchmarks. |
| Medium | Scanner status/latest report tools | Routine cron already uses the 3090; a typed way to inspect last run, freshness and alerts would make diagnosis easier. | Wrap the existing scanner’s stored run/results data without triggering scans or trades. No new financial-data provider is necessary for this initial addition. |
| Optional | GitHub PR/issues/CI access | Helpful for project summaries and following CI failures through to code changes. | No ARIA/Hermes GitHub MCP is configured. Use GitHub’s official server and select the needed repository, issue, pull-request and Actions tools; shell/gh access remains a separate existing route. [Official configuration](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md). |
| Optional | Linear | Useful only if Linear is an active source of project work. | Hermes has a disabled OAuth MCP entry; ARIA’s native Linear path has neither an enabled setting nor API key. Choose one primary integration and define project mappings for the native sync path. Linear supports a read-only endpoint as well as read/write access. [Official Linear MCP documentation](https://linear.app/docs/mcp). |
| Conditional | Semantic memory/document recall | Could help search old research using related concepts rather than exact terms. | Both embedding and search capabilities were deliberately disabled to save Mac resources. Preserve that decision unless revisited; re-enabling requires a resource budget and backfill verification. Snapshot backlog: 7 memories and 6 entities. |
| Conditional | Email/calendar | Relevant if ARIA is to manage a personal schedule or inbox. | No connection was found. Choose the actual account/provider and desired read/draft/write scope before configuring anything. |

No additional web-search provider is indicated by this audit: Tavily works, and ARIA also has a configured Brave credential for its research path. Credential presence alone does not certify that research path. Do not enable every available MCP toolset by default; retain Hermes’s working on-demand discovery.

## Model routing for Hermes auxiliary work

| Model | Configured tasks |
|---|---|
| NInfer-3090-Qwen3.8-27B | monitor, profile_describer, skills_hub, triage_specifier, tts_audio_tags, vision |
| Red-Qwen3.8-27B-PARO-INT5 | approval, background_review, compression, curator, kanban_decomposer, mcp, moa_aggregator, moa_reference, session_search, title_generation, web_extract |
| (auto/inherited) | goal_judge, memory_query_rewrite, review |

Main chat: PARO on Red. Routine cron: NInfer 3090. Auxiliary task entries describe routing configuration, not whether every corresponding optional feature is enabled or exercised.

## Complete outward-facing ARIA MCP inventory

All 127 discovered tools are grouped below; full descriptions and schemas are in `mcp-inventory.json`, and the combined inventory is in `all-tools.csv`. A listed tool is registered, not necessarily configured or tested. Linear is the known unconfigured external operation.

### Fleet and watched shells (14)

`fleet_status`, `list_shells`, `get_shell`, `get_shell_screen`, `get_shell_snapshot`, `get_shell_events`, `search_shells`, `send_shell_input`, `nudge_paused_shell`, `create_shell`, `delete_shell`, `set_shell_tags`, `resize_shell`, `wait_for_shell_output`

### Health and infrastructure (10)

`aria_health`, `tool_contract_status`, `operator_snapshot`, `health_services`, `host_temperatures`, `list_nodes`, `whats_running`, `list_services`, `start_service`, `stop_service`

### Projects, tasks and publishing (14)

`list_projects`, `get_project`, `projects_overview`, `project_cockpit`, `create_linear_ticket`, `publish_to_obsidian`, `retire_project`, `list_tasks`, `get_task`, `create_task`, `update_task`, `list_active_projects`, `get_project_charter`, `set_project_charter`

### Alerts and delivery bookkeeping (5)

`list_alerts`, `decide_alert`, `mark_alert_delivered`, `relay_heartbeat`, `ack_alert`

### Steward, guard, awareness and vault (12)

`steward_status`, `steward_runs`, `steward_tick`, `improve_status`, `improve_proposals`, `guard_status`, `guard_events`, `guard_checkpoints`, `vault_poll`, `vault_events`, `awareness_snapshot`, `awareness_observations`

### Coding sessions and recovery (16)

`checkpoint_coding_session`, `rollback_coding_session`, `coding_session_merge_gate`, `list_coding_sessions`, `create_coding_session`, `get_coding_session`, `wait_for_coding_session`, `get_coding_diff`, `get_coding_output`, `send_to_coding_session`, `stop_coding_session`, `set_coding_loop`, `resume_coding_session`, `review_coding_session`, `get_coding_review`, `set_coding_deadline`

### Model control and inference telemetry (18)

`inference_backend`, `inference_usage`, `inference_traces`, `list_model_servers`, `model_server_utilization`, `get_llm_route`, `set_llm_route`, `list_gpu_devices`, `start_model_server`, `stop_model_server`, `bind_model_server`, `unbind_model_server`, `sleep_model_server`, `pull_model`, `list_model_pulls`, `get_model_server`, `red_model_status`, `select_red_model`

### Memory and knowledge graph (12)

`kg_search`, `kg_entity`, `kg_map`, `kg_memories`, `search_memory`, `add_memory`, `retrieval_capabilities`, `set_retrieval_capabilities`, `list_memories`, `get_memory`, `update_memory`, `store_memory`

### Research, conversations and agents (8)

`chat`, `list_conversations`, `get_usage_cost`, `read_conversation`, `list_agents`, `update_agent`, `research_status`, `get_research_report`

### Durable coding loops (9)

`loop_status`, `loop_policy`, `get_loop_run`, `get_loop_logs`, `create_loop_run`, `update_loop_plan`, `approve_loop_run`, `extend_loop_limits`, `control_loop_run`

### Benchmarks (5)

`benchmark_status`, `benchmark_catalog`, `start_benchmark`, `get_benchmark_run`, `cancel_benchmark`

### Workflows (4)

`list_workflows`, `create_workflow`, `run_workflow`, `get_workflow_status`

## Complete internal ARIA router inventory

| Tool | Type | Generic API policy / observed issue |
|---|---|---|
| `filesystem` | builtin | Generic API sensitive execution disabled; agent path has a separate gate |
| `get_coding_diff` | builtin | Allowed by generic router policy; functional verification varies |
| `get_coding_output` | builtin | Allowed by generic router policy; functional verification varies |
| `list_coding_sessions` | builtin | Allowed by generic router policy; functional verification varies |
| `send_to_coding_session` | builtin | Not in generic router allowlist |
| `shell` | builtin | Generic API sensitive execution disabled; agent path has a separate gate |
| `start_coding_session` | builtin | Not in generic router allowlist |
| `stop_coding_session` | builtin | Not in generic router allowlist |
| `web_fetch` | builtin | Not in generic router allowlist; rejection verified live |
| `browse_page` | builtin | Allowed by generic router policy; functional verification varies |
| `check_runtime_updates` | builtin | Allowed by generic router policy; functional verification varies |
| `screenshot_analyze` | builtin | Not in generic router allowlist; implementation expects Linux DISPLAY/WAYLAND and a screenshot command |
| `generate_document` | builtin | Not in generic router allowlist; dependencies present, not exported through MCP |
| `update_soul` | builtin | Allowed by generic router policy; functional verification varies |
| `pi_coding_agent` | builtin | Allowed by generic router policy; functional verification varies |
| `browser_close` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_resize` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_console_messages` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_handle_dialog` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_evaluate` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_file_upload` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_drop` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_fill_form` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_press_key` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_type` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_navigate` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_navigate_back` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_network_requests` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_network_request` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_run_code_unsafe` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_take_screenshot` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_snapshot` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_click` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_drag` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_hover` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_select_option` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_tabs` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `browser_wait_for` | mcp | Allowed by generic router policy; functional verification varies; Playwright browser launch currently fails |
| `send_shell_input` | builtin | Not in generic router allowlist |

## Hermes native catalog and optional tools

**signal:** `browser_exec`, `clarify`, `computer_use`, `cronjob_manage`, `delegate_task`, `execute_code`, `memory`, `patch`, `process_manage`, `read_file`, `search_files`, `session_search`, `skill_manage`, `skill_view`, `skills_list`, `terminal`, `text_to_speech`, `todo_list`, `vision_analyze`, `web_extract`, `web_search`, `write_file`.

**cli:** `browser_exec`, `clarify`, `cronjob_manage`, `delegate_task`, `execute_code`, `memory`, `patch`, `process_manage`, `read_file`, `search_files`, `session_search`, `skill_manage`, `skill_view`, `skills_list`, `terminal`, `text_to_speech`, `todo_list`, `vision_analyze`, `web_extract`, `web_search`, `write_file`.

The old individual Hermes browser tools are replaced by `browser_exec` in this selected backend; their absence is not a separate missing-tool defect. `cronjob_manage` requires a gateway/interactive context; the final discovery probe set those flags, avoiding a false missing-cron report. Image generation and CLI Spotify failed availability selection. Kanban is an optional unavailable surface, not a reason to duplicate ARIA’s task system.

## Verification limits and artifacts

- No mutations such as model switching, task creation via MCP, rollback, merges, workflow runs, message sends or Linear writes were exercised. The audit itself was tracked as an ARIA task and published as documentation.
- Vision evidence comes from the immediately preceding same-day test: exact text, shapes and circle count were correct; ARIA traced the call to the 3090. Signal attachment transport was not retested.
- Audio services and credential validity for disabled integrations were not exercised. Stopped on-demand ARIA speech services do not establish that Hermes’s separate speech configuration is broken.
- Browser failure evidence is from the actual ARIA tool call and Hermes’s selected executable. No browser packages were installed or configurations changed.
- The currently running gateway’s readiness record and fresh-process catalog checks are recorded separately; a fresh probe does not certify every cached chat session.
- Inventory, service snapshots, minimal read-check outcomes, web results, PDF extraction, model routing and the complete tool CSV are stored beside this report. Credentials and private conversation content are excluded.
