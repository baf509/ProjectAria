# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Start for New Sessions

**Always start by reading these files in order:**
1. `PROJECT_STATUS.md` - Current phase and checklist
2. `CHANGELOG.md` (last 50 lines) - Recent changes
3. `SPECIFICATION.md` - Detailed architecture and requirements (now in Obsidian: `/home/ben/Obsidian/vault/ProjectAria/Specs/SPECIFICATION.md`)

> **Doc routing:** design / spec / analysis / research / planning docs live in the Obsidian vault at `/home/ben/Obsidian/vault/ProjectAria/` (synced to all devices). Agent-operational docs (this file, `PROJECT_STATUS.md`, `BACKLOG.md`, handoffs, READMEs) stay in the repo. See the `project-docs` skill.

## Architecture Overview

ARIA is a local-first agent **substrate/cockpit**, not a conversational front door. It owns
long-term memory, tool execution, the watched-shell fleet, and coding-session
orchestration — capabilities a human or agent *drives*, not something a human
chats with directly.

**Two ways to reach those capabilities (2026-07-28, clarified same day the
default chat agent was disabled):**
- **Hermes** (a separate agent, its own service) is the sole conversational/
  orchestrating agent between a human and ARIA. It reaches ARIA entirely
  through the **MCP server** (`mcp/server.py`) — ~40 tools wrapping `/api/v1`.
  When ARIA needs a new capability exposed to Hermes, add the MCP tool here
  and restart `hermes-gateway.service`; the `aria` MCP connection in Hermes's
  config has no per-tool whitelist, so a new tool becomes available on that
  restart alone — no separate Hermes-side registration needed (only Hermes's
  own *native* toolsets are explicitly whitelisted, for token-budget reasons).
- **The TUI is a direct operator cockpit** — a human drives ARIA's primitives
  (shells, coding sessions, fleet status) by hand, with no agent in the loop.
  This is manual control, not chat; it doesn't need and isn't meant to have an
  orchestrator persona.
- **ARIA's own default chat agent (`slug=aria`) is deliberately disabled**
  (`enabled=false` — see the `enabled` flag in `db/models.py`/`conversations.py`)
  — it was a third, redundant path that duplicated what Hermes already does,
  and its presence made it easy to accidentally reintroduce a human-facing
  ARIA chat surface. The Web UI and CLI chat commands still exist as code but
  hit the same disabled agent and are refused. Non-default agents used for
  actual work (`pi-coding` → local chadrockv2, `pi-coding-ridge` → Ridge,
  coding sessions) remain enabled — only the general-purpose default persona
  is off.

ARIA is the **single always-on service** on this host (`corsair-ai`). It listens on
**:8200** and has absorbed the former standalone `aria-shells` service — the
watched-shells / fleet subsystem now lives here (see *Watched Shells & Fleet*
below). The `aria-shells` repo is retained only as reference.

**Key principles:**
- **Linux service only** — ARIA runs exclusively as a service on a Linux machine. There is no native mobile/iOS client; the TUI/CLI/Web UI are operator/admin surfaces, not the primary way to interact with ARIA — that's Hermes, via the MCP server.
- **No framework dependencies** — No LangChain, LlamaIndex, LangGraph, or AutoGen. Direct API integration only.
- **Single-user design** — Personal agent, no multi-tenancy or auth.
- **LLM agnostic** — Adapter pattern for local llama.cpp servers, context-1, Anthropic, OpenAI, OpenRouter, and Fireworks. Backend + model are selected **per agent**.
- **Local-capable** — the default agents run on a **local** open-weights model on the GPU box; cloud backends are opt-in per agent.
- **MongoDB 8.2 + mongot** — Self-hosted vector search without Atlas subscription.

### Core Flow

```
User Message → API (FastAPI) → Orchestrator
    ├─ Context Builder (short-term + long-term memory)
    ├─ LLM Manager (selects backend, fallback chain)
    ├─ Tool Router (MCP + built-in tools)
    └─ Memory Extractor (background async)
→ Streaming Response (SSE)
```

### Memory System (Two-Tier)

1. **Short-term** (`conversations` collection): Recent conversation context via fast MongoDB queries. Current conversation + last 24h.
2. **Long-term** (`memories` collection): Hybrid search combining `$vectorSearch` (1024-dim voyage-4-nano) + `$search` (BM25) via RRF fusion (k=60). Background extraction from conversations via LLM.

### LLM Adapter Pattern

All backends implement `LLMAdapter` base class (`api/aria/llm/base.py`):
- `stream()` → async iterator of `StreamChunk` objects
- `complete()` → non-streaming completion
- Per-provider message format conversion and tool call support

Adapters: `llamacpp.py`, `context1.py`, `anthropic.py`, `openai.py`, `openrouter.py`, `fireworks.py`. The OpenRouter and Fireworks adapters use the OpenAI SDK internally (OpenAI-compatible); `fireworks.py` subclasses `OpenRouterAdapter` to reuse its GLM reasoning-mode handling. Manager (`manager.py`) handles backend selection and fallback chain.

**Current model topology** (the agents are config rows in `db.agents` — read them, don't trust this list blindly; as of 2026-07-28, two-server split + same-day routing correction + an evening crash that added a third server — full detail in `docs/ops/LOCAL_INFERENCE_TOPOLOGY.md` §10 and `vault/ProjectAria/Design/COHERENCE_DESIGN.md` §5 #24–28):
- **Three local model servers now, no shared KV cache between any of them.** `chadrock` (`:8102`, Laguna S 2.1 ROCmFP4/Vulkan, `-c 131072`) is the **`pool` CLI's dedicated server only** — nothing else may point at it, on purpose, since it's `--parallel 1`. `qwen3.6-35b-a3b` (`:8103`, Qwen3.6-35B-A3B-MTP ROCmFP4/Vulkan, renamed from `qwen-hermes`, `-c` trimmed to **100000**) serves **Hermes's main chat only** — `backend=llamacpp`/`agentic` still resolve here, but both consumers behind them (ARIA's default chat agent, Search Agent) are disabled, so it's single-active-consumer in practice. **`gemma-aux`** (`:8104`, Gemma 4 E4B Q4_0, **CPU-only**, new 2026-07-28 evening) took Hermes's ~16 auxiliary side-tasks + 2 cron jobs off qwen entirely. **⚠️ Docker/cgroup memory limits do not see GPU-offloaded memory on this unified-memory box** — `mem_limit` only works on gemma-aux (CPU-only); real pressure on chadrock/qwen must be read from `/sys/class/drm/card0/device/mem_info_gtt_{used,total}`, now monitored in `selfcheck.py`.
- **Pi Coding** is an external coding-session backend, not an ARIA chat persona. ARIA launches the real upstream `pi` executable in a watched tmux shell. The legacy `db.agents` rows are launch profiles: `pi-coding` selects **`provider=agentic`, Chadrockv2 Qwen3.6-27B ROCmFP6 on `:8105`**; `pi-coding-ridge` selects **`provider=ridge`, Qwen3.6-35B-A3B on Ridge's RTX 3090** via `ridge-llama-proxy`. Pi owns tools/transcript/context; ARIA owns the shell, worktree, fleet capture, concurrency, watchdog, review, and Ralph loop. A bare `backend="pi-code"` inherits `pi-coding`. `:8105` is stopped by default; start it through the registry before launching local Pi.
- **This host is LOCAL-ONLY as of 2026-07-26.** `OPENROUTER_API_KEY` is commented out in `.env` (credits exhausted, HTTP 402) and Fireworks is gone, so `GET /health` reports `available (llamacpp, agentic, ridge)`. **There is no cloud fallback anywhere.** Two settings that had been silently failing against the dead OpenRouter account are now local: `PLANNING_AMBIENT_BACKEND` (ambient task capture, fires on **every conversation turn**) and `HEARTBEAT_BACKEND`.
- **The old shared-`laguna` slot-proxy topology is retired.** `:8095`–`:8100` no longer listen; there's no per-agent slot pinning anymore because each server now has exactly the consumer set described above, not a pool of consumers to pin against.
- **Fireworks / GLM 5.2 is not in use.** `FIREWORKS_API_KEY` was removed from `.env` on 2026-07-23 after it began returning 401. The adapter and the `fireworks`/`glm` aliases remain — re-add a key to reactivate.
- The `qwen-rocmfp4` compose project under `infrastructure/` still defines **qwen-chat** `:8092` and **qwen-agentic** `:8093`; those containers are **RETIRED** (not deleted — profile-gated). ⚠️ `:8092` is bound by `ridge-llama-proxy` on the **tailnet IP only**, so `localhost:8092` is connection-refused even though `ss` shows a listener — this has caused misdiagnosis repeatedly.
- **As of 2026-07-29, ALL start/stop of these servers goes through ARIA's model-server registry (`api/aria/infrastructure/model_servers.py`), not manual `docker`/`docker compose` commands.** It tracks, per server, which llama.cpp fork/branch/commit + backend device (Vulkan/HIP/CPU) it needs — mixing a model with the wrong runtime either refuses to load or can wedge the GPU — plus a RAM-exclusivity group and a live-GTT-usage SWAG check, both of which hard-refuse `start()` unless `force=True`. Registry slugs (Ben's naming, tracks quant/runtime): `Laguna-S-2.1`, `Chadrock-Laguna-S-2.1`, `ROCmFP4-qwen3.6-35b-a3b`, `qwen3.6-35b-a3b-Q4`, `qwen3.6-27b-Q8`, `context1-Q4`, `gemma-4-e4b-Q4`, `Chadrock-ROCmFP6-qwen3.6-27b` (verified startable 2026-07-30, now bound to `pi-coding`), `Qwythos-27b-Q8` (startable, unbound — vision-capable), and off-box `Ridge-Qwen3.6-35B-A3B`. `bind()`/`unbind()` descriptively pair a server with an agent (`AgentResponse.model_server`, one-agent-per-server enforced) — it does not change the agent's actual `llm.backend`/`model` routing. API: `/api/v1/infrastructure/model-servers`; MCP: `list_model_servers`/`start_model_server`/`stop_model_server`/`bind_model_server`/`unbind_model_server`.

**Model pinning, cost & health:**
- A conversation can be pinned to a specific backend/model via `/model <backend> [<model-id>]` (strict — no fallback); `/model auto` unpins; `/route <task>` applies an advisory heuristic pin. Backend aliases include `agentic`/`qwen-agentic` and `fireworks`/`glm`.
- Cost accounting lives in `llm/pricing.py` (local backends = $0; cloud priced; unknown cloud → conservative default). Usage records carry `backend` + `session_id`; query via `GET /usage/cost`, `/usage/by-session`, `/usage/by-conversation`, `/usage/by-model`. A spend circuit-breaker (`spend_cap_usd_per_hour`, 0=off) trips the global e-stop when hourly priced spend exceeds the cap.
- `GET /health/services` concurrently probes the backing services that are *meant* to be up (mongod, mongot, qwen-chat, qwen-agentic, embeddings, tts, stt; context-1 and fireworks only when enabled/keyed). Disabled or unconfigured backends are omitted rather than counted as unhealthy; a `401`/`403` counts as **unhealthy** (a rejected credential is a real failure).

### Tool System

- **Built-in tools**: filesystem, shell, web (`api/aria/tools/builtin/`)
- **MCP integration**: stdio transport only, JSON-RPC 2.0 (`api/aria/tools/mcp/`)
- **Tool router**: Central registration, execution with 30s default timeout
- Orchestrator handles tool calls during LLM streaming, may trigger multiple rounds
- **Coding-session backends**: `start_coding_session(backend=...)` supports `claude_code`, `codex`, and `pi-code` (the real upstream `pi` executable with an explicit provider/model, supervised in the same watched-shell substrate as Claude Code). `browse_page` fetches a URL as readable text; full computer-use is available via the Playwright MCP `browser_*` family (gated by `tool_allowed_prefixes`).
- **Ralph loop (opt-in, per-session)**: a coding session carrying a `loop_config` is *nudged forward* by the watchdog whenever it idles at its prompt — re-checking killswitch/e-stop **every nudge** — until it emits the done token (`coding_loop_done_regex`, default `RALPH_DONE`) or hits `max_nudges`/`deadline_minutes`. Toggle via `POST /api/v1/coding/sessions/{id}/loop`, the MCP `set_coding_loop` tool (or `create_coding_session(loop=true)`), the `start_coding_session` `loop` param, or the TUI (`l` on the session or fleet screen). Absent `loop_config` = one-shot (unchanged). Settings live under `coding_loop_*` in `config.py`.

### Watched Shells & Fleet (`api/aria/shells/`, absorbed from aria-shells)

ARIA watches the `claude-*` tmux sessions you run and mines them for memories, a
project registry, and idle alerts.

- **Auto-adopt** — any tmux session named `claude-*` is picked up automatically.
  Real-time via the tmux hook (`scripts/aria-tmux-hook.conf` →
  `aria-shell-register --ensure-capture`), with `ShellAdoptWorker` (`adopt.py`)
  as a poll reconciler backstop. No explicit "create" needed.
- **Capture** — a `tmux pipe-pane` subprocess (`capture.py` via the
  `aria-shell-capture` shim) streams each line, ANSI-stripped, into
  `shell_events` with server-assigned line numbers.
- **Workers** — `snapshot` (pane rehydration), `extraction` (events → memories,
  with a per-call timeout + cursor self-heal), `prune` (per-shell token-budget
  scrollback retention), `selfcheck` (DB/LLM/embeddings/extraction health →
  alerts), `report` (weekly heartbeat). All gated by `settings` flags and wired
  in `main.py`'s lifespan.
- **Service API** — `ShellService.fleet_overview()` (one-call digest:
  status/idle/awaiting_input), `current_screen()` (live pane), `send_input(...,
  wait_ms=)` (act-and-observe → returns `(line, screen)`). Routes under
  `/api/v1/shells`.

### Planning: Projects & Tasks (`api/aria/planning/`)

One `projects` collection fed by **two** extractors: the ambient LLM
`TaskExtractor` (from conversations) and the deterministic `ProjectHarvestWorker`
(`shells/harvest.py`, from git repos + Claude/pi sessions + live shells). Human
`status` (lifecycle: active/paused/archived) is kept distinct from machine
`activity_status` (active/idle). To-dos live in `tasks`. Routes: `/api/v1/todos`,
`/api/v1/projects/{id|slug}`.

### Coherence Layer (implemented 2026-07-29 → 2026-08-02; design: `vault/ProjectAria/Design/COHERENCE_DESIGN.md`)

The work-coherence components hung off existing seams — "the bottleneck moved
from producing to maintaining coherence." All shipped except C5 (experiment
surface, deliberately deferred pending its own design conversation):

- **C1 Verification Gate** (`agents/watchdog.py` `_verify_session_done`/
  `_run_gate_check`): a Ralph-looped session's `RALPH_DONE` is checked by
  running `loop_config.gate_command` → `projects.check_command` → global
  `coding_gate_command` (`make check`) in the workspace before the loop ends;
  failure re-nudges with the check output (cap `coding_gate_max_retries` →
  alert `coding:gate`). Missing check → skip, never trap. Remote sessions run
  the check ON their node (C8). **`coding_gate_enabled=False` — deliberate
  opt-in default.** `gate_runs` history on the session doc.
- **C2 Repo-Change → Memory** (`shared/scan.py` `GitChangeEmitter`, on the S2
  scan worker): new commits since a per-repo cursor → `machine_scan` memories
  (private, LLM-free). **Enabled 2026-08-02** (`SHARED_SCAN_ENABLED=true`).
- **C3 Linear Reconciliation** (`planning/linear_sync.py`): mirrors mapped
  projects' open issues into `tasks` (read cache; Linear authoritative), LLM
  judge auto-resolves clearly-done tickets (≥0.9 conf + cited evidence,
  commented + reversible) or proposes (≥0.75); keep/kill/do-now routes under
  `/api/v1/linear/*`. **Dormant** until `LINEAR_ENABLED` + `LINEAR_API_KEY` +
  `linear_project_map` land; hand-verify the first live sync (GraphQL shapes
  are unexercised against the real API).
- **C4 Project Switcher + Cockpit** (`api/routes/digest.py`; web
  `/cockpit`; TUI `screenProjects`): `GET /projects/overview` ranks projects
  by attention (4·blocked + 3·gate-failed + 2·alerts + stale + running);
  `GET /projects/{slug}/cockpit` is the one-call per-project aggregate; the
  server-side active project lives in the fixed-`_id` `app_state` doc
  (`GET/PUT /projects/active`). Path→project attribution is
  **most-specific-root wins** (`PathIndex`) — never re-introduce plain prefix
  matching (a coarse parent row swallows children). Alerts carry an optional
  `project_path` for scoping.
- **C6 Obsidian** (`integrations/obsidian.py` `ObsidianWriter`): atomic,
  never-clobber, human-edit-guarded markdown into
  `vault/<RepoName>/<DocType>/`; research reports auto-publish; agents publish
  via `POST /api/v1/obsidian/publish`. Enabled (`OBSIDIAN_ENABLED=true`).
- **C8 Remote-node run_command** (`node/agent.py`, `ShellService.
  run_node_command`): `{exit_code, output_tail}` over the node command queue —
  server-internal only, never an agent tool. Live MacBook end-to-end
  verification still pending.
- **C9 Idle-Session Reaper** (`shells/reaper.py`): capture-then-reap ALL
  watched shells idle > `shells_reap_idle_days`, with **verified** save
  (HANDOFF.md modified after the prompt — a self-report is a claim, not a
  confirmation); unconfirmed save → skip-and-alert, never reap-anyway.
  Default OFF (`shells_reap_enabled`).
- **Nudge-paused-shells** (`api/routes/shell_nudge.py` +
  `POST /shells/{name}/nudge`): wakes a shell blocked at a prompt; attempts
  persist on the shell doc so the Hermes cron sweep ("ARIA paused-shell
  nudger", */15) gets three-strikes-across-runs; 3rd failure → alert
  `shells:nudge` → the triage cron relays a reply/STOP/IGNORE menu to Signal.
  Guards: killswitch/e-stop, `no-nudge` tag, 5-min min-paused, 10-min
  debounce. The did-it-work check is the NEXT sweep by design (the injected
  text resets the idle clock).

### MCP Server (`mcp/server.py`) — Hermes bridge

ProjectAria exposes an MCP server (FastMCP, run via `~/.local/share/aria-mcp/`,
launched by Hermes from `~/.hermes/config.yaml`). It surfaces **all of ARIA** to
Hermes — this is Hermes's *only* path to ARIA's capabilities (see *Architecture
Overview*) — ~40 tools wrapping `/api/v1`:
- **Fleet** — fleet_status, get_shell_screen, send_shell_input, create/delete/tag/resize, search, **nudge_paused_shell** (wake a blocked shell; three-strikes across calls → alert — see *Coherence Layer*).
- **Cockpit** — projects_overview (attention-ranked switcher), project_cockpit (per-project aggregate), set_active_project (shared focus).
- **Chat / agents** — chat (drive a non-default ARIA agent, e.g. pi-coding; the default `aria` agent is disabled), list/read conversations, list_agents, **update_agent** (enable/disable, repoint backend/model — addressed by slug).
- **Memory** — search_memory, add_memory.
- **Coding sub-agents** — list/create/get_output/send_to/stop coding sessions.
- **Projects / tasks** — native `/todos` + `/projects/{id|slug}`.
- **Alerts** — list_alerts, ack_alert.
- **Health / cost** — aria_health (quick, config-presence only), **health_services** (real per-backend reachability probes), **get_usage_cost** (spend by model/backend).
- **Model servers** — list_model_servers, start_model_server, stop_model_server, bind_model_server, unbind_model_server (the local LLM control plane — see *LLM Adapter Pattern* above).
- **Long-form / trackers** — publish_to_obsidian (guarded markdown into the vault, C6), create_linear_ticket (Signal → Hermes → Linear capture path, C3; 409 while Linear is disabled).

After editing `mcp/server.py`, restart `hermes-gateway.service` to reload the toolset —
the `aria` MCP connection has no per-tool whitelist on Hermes's side, so this
restart alone is sufficient; no config.yaml edit is needed to "register" a new tool.

### Coding Sub-agents on the Shell Substrate (`api/aria/agents/`)

ARIA-spawned coding sessions (`start_coding_session`, watchdog, checkpoints,
review) run **on the watched-shell substrate** by default
(`coding_use_shell_substrate`): `session.py` creates a `claude-coding-*` tmux
shell via `ShellService` (interactive, not `-p` batch), so a sub-agent **is** a
watched shell — auto-captured, in the fleet/TUI, and drivable via the same
tools. `get_output`/`send_input`/`stop` route to the shell; the
watchdog/checkpoint/review overlay still manages it through the manager
interface. Subprocess + visible-tmux substrates remain as fallbacks. A session
can be kept running via the **Ralph loop** (see *Coding-session backends* above):
the watchdog (`agents/watchdog.py` `_maybe_nudge`) re-feeds an idle session until
it signals done or trips a cap — driven through the same `send_input`, so it works
for any substrate and inherits the safety gates.

**Concurrency limiter (Pi-Flow parity).** A session holds a "slot" while it is
actively running; spawns beyond `coding_max_concurrent_sessions` (default 4;
0 = unbounded) sit in a `queued` state and launch as slots free.
`coding_queue_max` (default 64) hard-caps the wait queue. The gate is CV-guarded
with idempotent, set-based slot release across every finalize path; a free slot
launches inline (unchanged fast path). Applies to all substrates including
pi-code. A slot is held for a session's whole active life, so long-lived Ralph
sessions occupy one — size the cap accordingly. Gauge:
`GET /api/v1/coding/sessions/concurrency` (also merged into MCP `fleet_status`).
**Join primitive:** `CodingSessionManager.wait_for_session(id, timeout)` polls a
session to a terminal state (restart-safe) and returns its `result_summary` —
the building block workflow fan-out consumes.

**Specialist profiles.** `start_coding_session(subagent_profile=<slug>)` resolves
a `db.agents` row and applies its backend/model + `system_prompt` (role
preamble); an explicit backend/model still wins.

### Workflows: fan-out orchestration (`api/aria/workflows/engine.py`)

`WorkflowEngine` runs a top-level **linear DAG** (conditions, `depends_on`,
`{{steps.N.path}}` interpolation) plus **fan-out** actions: `parallel` (concurrent
explicit sub-steps, bounded by `max_concurrent`), `map` (one `template` over a
list, with `{{item}}`/`{{index}}` scope), `code_session` with `await:true` (join a
spawned sub-agent via `wait_for_session`, capturing `result_summary`), and
`synthesize` (reduce prior results into one answer via an agent turn — optional
`backend`/`model`, e.g. merge on Opus). Sub-step results nest under the group as
`results`/`records`, addressable via `{{steps.N.results.M.path}}` (dotted paths
walk lists too). Routes under `/api/v1/workflows`; exposed to Hermes as MCP
`list_workflows`/`create_workflow`/`run_workflow`/`get_workflow_status`. Together
these give Pi-Flow-style parallel research, multi-model review, staged pipelines,
and synthesized results on ARIA's existing session + mailbox primitives.
**Standing design decision (Pi-Flow parity, 2026-07-25):** extend this
declarative JSON engine rather than embed a JS/sandbox workflow runtime —
reuses persistence/recovery/REST/TUI and honors the no-framework rule. Revisit
only if declarative proves too limiting.

### Complexity Routing (`api/aria/agents/routing.py`)

A coding task started with **no explicit backend/model** is classified into a
tier and run on that tier's model — `deep` (planning/design/strategy) → Opus 4.8,
`standard` (scoped implementation) → Sonnet 5, `light` (research) → Sonnet 5.
Sonnet is the floor; the sub-Sonnet fallback (`pi-code` on the local
open-weights server) is reachable only when the Claude quota is cooling down. Three stages, cheap-first: a
heuristic prefilter, then a Sonnet-class judge (`coding_routing_judge_transport`:
`api` for the interactive path, `cli` to burn the subscription instead of API
tokens), then an availability check. Any failure degrades to `standard` — routing
never blocks a spawn. The verdict is persisted on the session doc as `routing`.

**What counts as a pin:** an explicit `model` always wins and skips routing. An
explicit *backend* only skips routing when it's one the router wouldn't have
picked itself (`codex`, `pi-code`) — `backend="claude_code"` is agreement, not an
override, so routing still runs (`routing.is_routable_backend`). This matters
because Hermes passes `backend=claude_code` as belt-and-suspenders, which used to
silently disable routing for every Hermes-originated task.

Because routing happens inside `start_session()`, every caller inherits it —
including remote-node sessions, since `--model` is already part of the launch
string shipped to `aria-node`. `POST /api/v1/routing/classify` exposes the same
decision to thin clients.

**Desk path — routing deliberately NOT wired in (decided 2026-07-24).** Claude
Code is one-model-per-session (you pick at launch or `/model` inside; no hook can
swap the model per prompt), so an interactive REPL — the sit-down `claude`
workflow — has no single task to classify and can't be dynamically re-routed
mid-session. Auto-routing the desk path was tried and reverted: it fit awkwardly,
and the primary habit (`claude --dangerously-skip-permissions`) bypassed it
anyway. So bare `claude` on corsair is just the saved-state per-directory shell
attach (`claude()` in `~/.bashrc`); you choose the model. Since 2026-08-02,
`codex` gets the same treatment: a `codex()` wrapper in `~/.bashrc` spawns a
persisted watched session named `claude-codex-<dir>` via `POST /api/v1/shells`
with `launch_command` → `scripts/aria-codex-launch` (resume-aware,
`codex resume --last` is cwd-filtered, always `--yolo`). Routing lives **only on
the automated spawn path** (`start_session()` — Hermes/MCP/TUI create), where one
task genuinely is one session.

The desk-path scripts (`scripts/aria-claude.sh`, `scripts/aria-route-task`,
`scripts/aria-desk-install-mac`) are **retained but not sourced** — `aria-route-task`
is still a useful manual "what would this route to?" client against
`POST /api/v1/routing/classify`. Re-source `aria-claude.sh` to bring back
`claude "<task>"` auto-routing if the trade-off ever looks different.

**Quota:** ARIA cannot see the Claude subscription quota (no API exists). The
watchdog records a cooldown in `model_availability` when it sees rate-limit text
in a `claude_code` session's output; the router demotes until it expires.

### Notifications, Alerts & Self-Healing (`api/aria/notifications/`)

ProjectAria does **not** push notifications itself (no Signal/Telegram send path; Telegram was removed entirely). `NotificationService.notify()`
enqueues cooldown-gated, **actionable** alerts into the `alerts` collection (it
**drops** `coding:*` / `task` lifecycle events — those aren't alerts, and
enqueuing them would loop the triage below). `selfcheck` alerts **once per state
transition** (degraded → recovered), not every tick.

Hermes owns the **self-healing loop** (a cron job, `~/.hermes/cron/jobs.json`):
on each unacked alert it spawns a diagnostic coding sub-agent via the aria MCP,
collects a root-cause + proposed fix, relays *that* to Signal ("reply APPLY…"),
and acks. On APPLY, Hermes spawns a fixer agent. Routes: `/api/v1/alerts`
(`list_alerts` / `ack_alert`).

## Shared Infrastructure

ARIA depends on shared infrastructure at `/home/ben/Development/infrastructure/` (also used by AgentBenchPlatform). **Must be started first.**

| Service | Port | Purpose |
|---------|------|---------|
| mongod | 27017 | MongoDB 8.2 data (replica set `rs0`) |
| mongot | 27028 | MongoDB search (vector + text) |
| laguna | 8095 | local LLM — `laguna-s-2.1` Q4_K_M (ROCm). **Currently serves both `llamacpp_url` and `agentic_url`** |
| embeddings | 8001 | voyage-4-nano via sentence-transformers (CPU) |
| qwen-chat | 8092 | local LLM — Qwen3.6 **35B-A3B** (ROCm). Defined but *not running* |
| qwen-agentic | 8093 | local LLM — Qwen3.6 **27B** (ROCm). Defined but *not running* |
| context-1 | 8081 | local LLM — chromadb/context-1 20B (Search Agent backend). *Not running; disabled via `CONTEXT1_ENABLED=false`* |

> The local LLM containers live under `infrastructure/`: `laguna`
> (`laguna-rocm:latest`) is the one actually serving traffic; the
> `qwen-rocmfp4/` compose project (`qwen-chat` / `qwen-agentic` / `context1`,
> image `qwen-rocmfp4:latest`) is defined but down. The old single `llamacpp` on
> `:8080` is **retired** (behind the compose `legacy` profile). To add/restart a
> qwen model, edit `qwen-rocmfp4/docker-compose.yml` and
> `docker compose up -d <service>`.
>
> **⚠️ Do NOT "fix" `LLAMACPP_URL` back to a model port (2026-08-05).** It is
> deliberately `http://localhost:8200/llm/v1` — ARIA's own OpenAI-compatible
> passthrough (`api/routes/llm_proxy.py`), which forwards to whichever on-box
> server is currently resident (largest `resident_gib` wins). Pinning a port
> here broke four times running (qwen → laguna → chadrock → DS4): the big
> servers are mutually RAM-exclusive, so the named one goes down the moment
> another starts, and `selfcheck` then pages `llm (ConnectError)` every 10
> minutes into the Hermes alert-triage cron. `LLAMACPP_API_KEY` must equal
> `API_KEY` (the middleware accepts it as `Authorization: Bearer`). To swap the
> *resident* model, start/stop servers through the model-server registry — the
> `llamacpp` backend follows automatically. `AGENTIC_URL` still names a specific
> port (`:8105`) because it is the on-demand local coding server.
>
> **Hermes follows the same passthrough (2026-08-05).** `~/.hermes/config.yaml`
> no longer names a model: `model.default: aria-resident` /
> `model.provider: custom:aria` → `base_url: http://localhost:8200/llm/v1`. So
> **starting a model in ARIA is the whole swap** — no Hermes config edit, no
> gateway restart. Don't "fix" that base_url to a model port; it is ARIA on
> :8200, and ARIA is the only thing that knows whether the resident server is
> loopback- or tailnet-bound. Reverting to a fixed model means restoring
> `provider: custom:ds4` (see `config.yaml.bak-aria-follow-*`).
>
> **Which model serves, when several are resident.** More than one can be up
> (gemma is CPU-only and coexists; chadrock+qwen is a deliberate pair), so the
> proxy resolves in order: the request's `model` field when it names a running
> server → an operator pin → largest `resident_gib`. llama.cpp ignores unknown
> `model` values, which is what frees the field to act as a selector. Naming a
> *stopped* server is a 503; a *stale pin* degrades to auto (and says so). Set
> the pin on the web `/operate` "Local model route" panel, via
> `GET`/`PUT /api/v1/infrastructure/llm-route`, or MCP
> `get_llm_route`/`set_llm_route`. `GET /llm/v1/models` lists everything loaded
> with each model's real `n_ctx`. Selection logic + its tests:
> `infrastructure/llm_route.py`, `tests/test_llm_route.py`.

```bash
# Start shared infra first
cd /home/ben/Development/infrastructure && docker compose up -d

# Start ARIA API (native systemd service)
systemctl --user start aria-api

# Start ARIA Docker services (tts, stt, ui)
cd /home/ben/Development/ProjectAria && docker compose up -d
```

**Connection string**: `mongodb://mongod:27017/?directConnection=true&replicaSet=rs0`

Search indexes are created via `infrastructure/scripts/init-mongo.js`:
- `memory_vector_index` — vector search (1024 dims, cosine)
- `memory_text_index` — BM25 lexical search

## Development Commands

### API (FastAPI backend — native systemd service)

```bash
# The API runs natively (not in Docker) for filesystem/process access.
# Managed via systemd user service:
systemctl --user start aria-api     # Start
systemctl --user stop aria-api      # Stop
systemctl --user restart aria-api   # Restart
systemctl --user status aria-api    # Check status
journalctl --user -u aria-api -f   # View logs

# For development with auto-reload (stop the systemd service first, or use a
# spare port, since the live service already binds :8200):
cd api
uvicorn aria.main:app --reload --host 0.0.0.0 --port 8200
# Docs at http://localhost:8200/docs
```

### UI (Next.js)

```bash
cd ui
npm install
npm run dev          # Dev server at http://localhost:3000
npm run build        # Production build
```

### Desktop Widget (Tauri v2)

```bash
cd widget
npm install
npm run tauri:dev    # Dev mode with hot-reload
```

### CLI

```bash
cd cli
pip install -e .
aria chat "Hello ARIA!"
aria chat --conversation <id> "Continue"
aria conversations list
aria memories search "query"
aria tools list
aria mcp list
aria tui                       # launch the Go TUI (the cockpit)
aria tui --host corsair        # remote cockpit: point at another host (see below)
```

### Cross-machine cockpit (TUI)

The Go TUI (`tui/`) is a thin **pure-HTTP** client with no machine-local
assumptions, so it doubles as a **remote cockpit**: run it on another machine
(e.g. the MacBook) and point it at corsair over the tailnet — no SSH.
`aria tui --host <name|host:port|url>` (or `aria-tui --host …` for the raw binary)
resolves profiles from `~/.config/aria/hosts`; resolution precedence is flag →
`ARIA_API_URL`/`.env` → `default` profile → `http://localhost:8200`. Build the
Apple-Silicon binary with `cd tui && make build-darwin` (see **`tui/README.md`**
for the Taildrop/scp transfer + one-time ad-hoc `codesign` recipe). See *Multi-machine fleet* below for making the *fleet itself* span machines
(designed in **`MULTI_MACHINE_FLEET_DESIGN.md`**).

### Multi-machine fleet (`api/aria/nodes/`, `api/aria/node/`)

The watched-shell fleet can span this host plus remote **nodes** (e.g. a MacBook).
A remote `aria-node` agent (`python -m aria.node` / `scripts/aria-node`, outbound-
only, httpx + tmux, no Mongo) registers via `/api/v1/nodes/*`, captures its local
`claude-*` shells (pushing events/snapshots), and long-polls a `shell_commands`
queue to be driven back. `ShellService` is **host-aware**: `send_input` /
`current_screen` / `session_alive` / `kill_shell` dispatch by the shell's `host`
— local → tmux (unchanged); remote → the node queue (`_remote_command`). Reads
(`fleet_overview`, scrollback) are host-agnostic. `start_coding_session(host=<node>)`
runs a coding session on that node; the watchdog + **Ralph loop drive it over the
wire for free**. `local_node_id` (config, default hostname) identifies this host;
corsair's own shells keep the direct-local fast path (zero regression). Both
layers are **implemented**; live end-to-end needs an `aria-api` restart. See
`MCP: list_nodes` + `create_coding_session(host=…)` and the TUI fleet HOST column.

### Docker Compose

```bash
docker compose up -d           # Start ARIA Docker services (tts, stt, ui)
docker compose ps              # Check health
docker compose logs -f tts     # View logs
docker compose down            # Stop

# API is managed separately via systemd:
systemctl --user start aria-api
```

### Database

```bash
mongosh mongodb://localhost:27017/?directConnection=true&replicaSet=rs0
# use aria → show collections → db.memories.getSearchIndexes()
```

## ARIA Services

| Service | Port | How it runs | Description |
|---------|------|-------------|-------------|
| api | 8200 | systemd user service (`aria-api`) | FastAPI backend (native, not Docker). Binds :8200 via a drop-in override (`~/.config/systemd/user/aria-api.service.d/override.conf`); the old :8000 is retired. |
| ui | 3000 | Docker (docker-compose.yml) | Next.js web UI (built against `NEXT_PUBLIC_API_URL` → :8200) |
| tts | 8002 | Docker (docker-compose.yml) | Qwen3-TTS 0.6B speech synthesis (CPU) |
| stt | 8003 | Docker (docker-compose.yml) | whisper-large-v3-turbo transcription (CPU, int8) |
| mcp | stdio | launched by Hermes | `mcp/server.py` — MCP bridge over `/api/v1` for the Hermes agent |
| tmux | — | systemd user service (`aria-tmux`) | Owns the tmux server that hosts every watched `claude-*` session. Ordered before `aria-api`. See the gotcha below — **never** let aria-api spawn the server. |

## Code Patterns

### Python File Headers

```python
"""
ARIA - [Module Name]

Phase: [Phase number(s)]
Purpose: [One-line description]

Related Spec Sections:
- Section X.Y: [Description]
"""
```

### Async Everywhere

All database and network operations must be async (motor for MongoDB, httpx for HTTP).

### FastAPI Dependency Injection

Dependencies are wired through `api/aria/api/deps.py`. The orchestrator, tool router, and MCP manager are injected into route handlers via `Depends()`.

### Streaming Responses

SSE via `sse-starlette`. The orchestrator yields `StreamChunk` objects that are serialized to SSE events.

## Approved Libraries

`httpx`, `motor`, `pydantic`, `fastapi`, `anthropic`, `openai`, `sse-starlette`, `sentence-transformers`

## Ops runbooks (repo-local)

- **`docs/ops/LOCAL_INFERENCE_TOPOLOGY.md`** — which port each ARIA consumer must
  use, the laguna slot map, which background workers actually cost tokens, and
  the retired-endpoint hazards (`:8092` is bound on the tailnet IP only, so
  `localhost:8092` is refused even though a listener exists). **Read before
  changing any `*_URL` or adding a worker that calls an LLM.**
- Companions outside this repo: `infrastructure/laguna/LAGUNA_TUNING_20260726.md`
  (server benchmarks, flash-attention crash, slot semantics) and
  `Development/Hermes/HERMES_TUNING_20260726.md` (Hermes config, prompt-cache
  root cause, tool trimming).

## Critical Gotchas

### The tmux server must be owned by `aria-tmux.service` (DO NOT let aria-api spawn it)

A tmux server inherits the cgroup of the **client that first invokes tmux**. If
`aria-api` wins that race (its adopt/spawn path calls tmux constantly), the
server lands in `aria-api.service` — and systemd's default
`KillMode=control-group` then destroys the server, and **every watched
`claude-*` session with it**, on each `systemctl --user restart aria-api`. This
silently ate whole days of live sessions before it was found.

`aria-tmux.service` (`scripts/aria-tmux-server`) exists solely to own that
server, and `aria-api.service.d/tmux-ordering.conf` makes aria-api `Want`/`After`
it, so a server always pre-exists and aria-api only connects as a client.

- `exit-empty off` is **load-bearing** — at the tmux default (`on`) the server
  exits with its last session and the next caller rebuilds it in *their* cgroup.
- Verify ownership at any time:
  `systemd-cgls --user-unit aria-tmux.service` — the `tmux` process must appear there.
- Pane processes were never at risk: tmux 3.4 already puts each pane in its own
  transient `tmux-spawn-*.scope`. Only the *server* was captured.

### Embedding Dimensions (DO NOT CHANGE)

Model is `voyageai/voyage-4-nano` with **1024-dim MRL truncation**. The MongoDB vector index, embedding service, and all stored memories must use exactly 1024 dimensions. Changing requires full re-embedding of all memories.

**Storage format (as of 2026-07-18, Shared Services S5):** embeddings are stored as MongoDB's **native BSON vector type (Binary subtype 9, float32)** via `Binary.from_vector` — *not* the old `struct.pack` subtype-0 blobs. `binary_to_embedding` still decodes legacy subtype-0 docs for safety. Re-encoding format is fine; changing *dimensions/model* is what's forbidden.

### Memory HTTP API (cross-machine, Shared Services S1)

`POST /api/v1/memory/recall {query,k}` and `POST /api/v1/memory/store {content,type,...}` wrap `LongTermMemory` and embed server-side, so thin clients on other machines can recall/store without a local venv. Both require the global `X-API-Key`. Machine-state + git scanning (S2 — **enabled 2026-08-02**, `SHARED_SCAN_ENABLED=true`, emitters `MachineScanMemoryEmitter` + `GitChangeEmitter`) and the review surface (`/api/v1/shared/review`, S3) live under `api/aria/shared/`.

**S3 ownership convention (applies everywhere hybrid human/machine data meets):** workers write only structural/derived fields and never overwrite human/agent-curated ones; on contradiction, propose-for-review (`merge_owned`, `shared/ownership.py`) rather than clobber; vanished things go `stale`, never deleted. The ObsidianWriter's human-edit guard and C3's propose-don't-clobber are the same rule. *(The `SHARED_SERVICES_DESIGN.md` vault doc was retired 2026-08-02 — S1–S5 all shipped; this section and the code docstrings are its surviving record.)*

### Shared Infrastructure

- **Start infra first** — ARIA services depend on it
- **Replica set required** — Search features only work with `replicaSet=rs0`
- **Connection string** — Must include `directConnection=true&replicaSet=rs0`
- **Shared Docker network** — Services use `shared-infra` network; use container names (e.g., `mongod`, `embeddings`) not `localhost` in Docker contexts
- **Stopping infra affects AgentBenchPlatform** — both projects share these services
- **Security posture (S4, deliberate):** Mongo (`27017`) and `:8200` are bound to `0.0.0.0` with no per-service auth — safe only because this box lives on a closed tailnet. Writes are gated by the global `X-API-Key`; deeper hardening (re-binding off `0.0.0.0`) was explicitly ruled out of scope because existing tailnet clients hit Mongo directly. Don't "fix" this without Ben.

### When Making Changes

1. Check current phase in `PROJECT_STATUS.md`
2. Read relevant section in `SPECIFICATION.md`
3. Follow established code patterns
4. Update `CHANGELOG.md` with changes
5. Update `PROJECT_STATUS.md` if completing checklist items

## Testing

```bash
cd api
python3 -m pytest tests/ -v        # Run all tests
python3 -m pytest tests/ -k "tool"  # Run tests matching keyword
```

Test suite covers: tokenizer, resilience (retry/circuit breaker), tool router (registration, policy, execution, audit), LLM base classes, memory RRF fusion, orchestrator command parsing (mode/research/memory/coding), research service (JSON parsing, deduplication, HTML stripping), workflow engine (conditions, dependencies, parameter interpolation, and fan-out: parallel/map/synthesize + code_session await), and the coding-session concurrency limiter + `wait_for_session` join primitive.

Additional manual testing via CLI, API docs (`/docs`), and Docker Compose integration.
