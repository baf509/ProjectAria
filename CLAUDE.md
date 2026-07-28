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
  through the **MCP server** (`mcp/server.py`) — ~35 tools wrapping `/api/v1`.
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
  actual work (`pi-coding` → Ridge, coding sessions) remain enabled — only the
  general-purpose default persona is off.

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
- **Pi Coding Agent** (`slug=pi-coding`, chat-only tool) and **Pi Coding Agent (Ridge)** (`slug=pi-coding-ridge`, has filesystem/shell tools) **both run on Ridge's RTX 3090** (`backend=ridge`, via `ridge-llama-proxy`) — laguna/chadrock no longer backs anything named "pi-coding". A bare `backend="pi-code"` coding session with no `subagent_profile` resolves to the `pi-coding` row and lands on Ridge the same as an explicit `pi-coding-ridge` profile.
- **This host is LOCAL-ONLY as of 2026-07-26.** `OPENROUTER_API_KEY` is commented out in `.env` (credits exhausted, HTTP 402) and Fireworks is gone, so `GET /health` reports `available (llamacpp, agentic, ridge)`. **There is no cloud fallback anywhere.** Two settings that had been silently failing against the dead OpenRouter account are now local: `PLANNING_AMBIENT_BACKEND` (ambient task capture, fires on **every conversation turn**) and `HEARTBEAT_BACKEND`.
- **The old shared-`laguna` slot-proxy topology is retired.** `:8095`–`:8100` no longer listen; there's no per-agent slot pinning anymore because each server now has exactly the consumer set described above, not a pool of consumers to pin against.
- **Fireworks / GLM 5.2 is not in use.** `FIREWORKS_API_KEY` was removed from `.env` on 2026-07-23 after it began returning 401. The adapter and the `fireworks`/`glm` aliases remain — re-add a key to reactivate.
- The `qwen-rocmfp4` compose project under `infrastructure/` still defines **qwen-chat** `:8092` and **qwen-agentic** `:8093`; those containers are **RETIRED** (not deleted — profile-gated). ⚠️ `:8092` is bound by `ridge-llama-proxy` on the **tailnet IP only**, so `localhost:8092` is connection-refused even though `ss` shows a listener — this has caused misdiagnosis repeatedly.

**Model pinning, cost & health:**
- A conversation can be pinned to a specific backend/model via `/model <backend> [<model-id>]` (strict — no fallback); `/model auto` unpins; `/route <task>` applies an advisory heuristic pin. Backend aliases include `agentic`/`qwen-agentic` and `fireworks`/`glm`.
- Cost accounting lives in `llm/pricing.py` (local backends = $0; cloud priced; unknown cloud → conservative default). Usage records carry `backend` + `session_id`; query via `GET /usage/cost`, `/usage/by-session`, `/usage/by-conversation`, `/usage/by-model`. A spend circuit-breaker (`spend_cap_usd_per_hour`, 0=off) trips the global e-stop when hourly priced spend exceeds the cap.
- `GET /health/services` concurrently probes the backing services that are *meant* to be up (mongod, mongot, qwen-chat, qwen-agentic, embeddings, tts, stt; context-1 and fireworks only when enabled/keyed). Disabled or unconfigured backends are omitted rather than counted as unhealthy; a `401`/`403` counts as **unhealthy** (a rejected credential is a real failure).

### Tool System

- **Built-in tools**: filesystem, shell, web (`api/aria/tools/builtin/`)
- **MCP integration**: stdio transport only, JSON-RPC 2.0 (`api/aria/tools/mcp/`)
- **Tool router**: Central registration, execution with 30s default timeout
- Orchestrator handles tool calls during LLM streaming, may trigger multiple rounds
- **Coding-session backends**: `start_coding_session(backend=...)` supports `claude_code`, `codex`, and `pi-code` (ARIA's own agentic loop with a pinned `llm`/`model`, supervised by the watchdog + e-stop/killswitch). `browse_page` fetches a URL as readable text; full computer-use is available via the Playwright MCP `browser_*` family (gated by `tool_allowed_prefixes`).
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

### MCP Server (`mcp/server.py`) — Hermes bridge

ProjectAria exposes an MCP server (FastMCP, run via `~/.local/share/aria-mcp/`,
launched by Hermes from `~/.hermes/config.yaml`). It surfaces **all of ARIA** to
Hermes — this is Hermes's *only* path to ARIA's capabilities (see *Architecture
Overview*) — ~35 tools wrapping `/api/v1`:
- **Fleet** — fleet_status, get_shell_screen, send_shell_input, create/delete/tag/resize, search.
- **Chat / agents** — chat (drive a non-default ARIA agent, e.g. pi-coding; the default `aria` agent is disabled), list/read conversations, list_agents, **update_agent** (enable/disable, repoint backend/model — addressed by slug).
- **Memory** — search_memory, add_memory.
- **Coding sub-agents** — list/create/get_output/send_to/stop coding sessions.
- **Projects / tasks** — native `/todos` + `/projects/{id|slug}`.
- **Alerts** — list_alerts, ack_alert.
- **Health / cost** — aria_health (quick, config-presence only), **health_services** (real per-backend reachability probes), **get_usage_cost** (spend by model/backend).

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
attach (`claude()` in `~/.bashrc`); you choose the model. Routing lives **only on
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
> `:8080` is **retired** (behind the compose `legacy` profile). To swap which
> model backs a backend, change `LLAMACPP_URL` / `AGENTIC_URL` in `.env` and
> restart `aria-api`; to add/restart a qwen model, edit
> `qwen-rocmfp4/docker-compose.yml` and `docker compose up -d <service>`.

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

`POST /api/v1/memory/recall {query,k}` and `POST /api/v1/memory/store {content,type,...}` wrap `LongTermMemory` and embed server-side, so thin clients on other machines can recall/store without a local venv. Both require the global `X-API-Key`. Machine-state scanning (S2, `shared_scan_enabled`) and the review surface (`/api/v1/shared/review`, S3) live under `api/aria/shared/`.

### Shared Infrastructure

- **Start infra first** — ARIA services depend on it
- **Replica set required** — Search features only work with `replicaSet=rs0`
- **Connection string** — Must include `directConnection=true&replicaSet=rs0`
- **Shared Docker network** — Services use `shared-infra` network; use container names (e.g., `mongod`, `embeddings`) not `localhost` in Docker contexts
- **Stopping infra affects AgentBenchPlatform** — both projects share these services

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
