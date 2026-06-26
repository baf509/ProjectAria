# ARIA - Autonomous Reasoning & Intelligence Architecture

> Personal AI agent with persistent memory, autonomous sub-agents, background processes, and multiple interfaces — runs as a self-hosted service on your Linux machine.

ARIA is not a chatbot. She's a self-hosted AI agent with her own evolving identity ([SOUL.md](api/prompts/)), long-term memory, the ability to spawn autonomous coding agents, and background processes that run while you sleep. She works with any LLM backend and runs entirely on your infrastructure.

## What Makes ARIA Different

Most AI chat apps are stateless wrappers around an API. ARIA is an **agent platform**:

- **She remembers everything** — Hybrid vector + lexical search across all conversations, all interfaces
- **She can delegate work** — Spawns Claude Code or Codex sessions that code autonomously with `--dangerously-skip-permissions`
- **She runs background processes** — Dreams, research, heartbeat checks, self-correction, all running as autonomous Claude Code subprocesses
- **She's everywhere** — Same agent accessible from terminal, browser, desktop widget, Signal, Telegram, or REST API
- **She evolves** — Dream cycle reflects on memories, proposes changes to her own identity (with your approval)

## Architecture

```
                    You
                     |
        +------------+------------+
        |            |            |
     Web UI     TUI (Go)    Widget/CLI/Signal/Telegram
        |            |            |
        +------------+------------+
                     |
              +------+------+
              |    ARIA     |
              | Orchestrator|
              +------+------+
                     |
       +-------------+-------------+
       |             |             |
  Sub-Agents    Background     Tools &
  (Coding)      Processes       MCP
       |             |             |
  Claude Code   Dream Cycle    Filesystem
  Codex         Research       Shell
  Pi Coding     Heartbeat      Web Fetch
                Autopilot      Screenshot
                Awareness      Skills
                OODA Loop      MCP Servers
                Summarization
                Memory Extract
```

Every user message flows through the orchestrator, which assembles context (identity, memories, conversation history, awareness data), streams through the LLM with tool execution, and fires background tasks (memory extraction, summarization) after each exchange.

## Sub-Agents

ARIA delegates specialized work to autonomous sub-agents. These persist until you explicitly close them — no timeouts.

### Claude Code Sessions

Interactive coding sessions using the Claude Code CLI. ARIA spawns these as autonomous subprocesses with full filesystem and shell access.

- **Binary**: `claude --dangerously-skip-permissions`
- **Monitoring**: Watchdog checks every 5s, notifies on stalls, auto-responds to prompts
- **Review**: Auto-runs tests, lint, and git diff when sessions complete
- **Persistence**: Sessions stay alive until the process exits or you stop them

### Codex Sessions

Same infrastructure as Claude Code but using OpenAI's Codex CLI with `--sandbox workspace-write --ask-for-approval never`.

### Pi Coding Agent

A coding-assistant persona. ARIA creates a persistent conversation and processes it through her own orchestrator. (Its model is configurable per agent — currently GLM 5.2 via Fireworks; it can be pointed at a local qwen backend instead.)

> **Note on coding sub-agents:** ARIA-spawned coding sessions now run on the **watched-shell substrate** — each becomes an interactive `claude-coding-*` tmux shell, captured and visible in the fleet/TUI and drivable via the same tools, with the watchdog/checkpoint/review overlay still managing it.

## Watched Shells

ARIA can observe and interact with tmux sessions *you* own — separate from the coding sub-agents she spawns herself. Point her at your Claude Code or Codex sessions and she gains situational awareness of what you're working on, can answer prompts for you, and extracts memories from the conversations.

> Originally a separate service (`aria-shells`), this subsystem was absorbed back
> into ARIA so it is the single always-on service. It is also surfaced to the
> remote **Hermes** agent through ARIA's MCP server (see *MCP Bridge* below).

**How it works:**
- **Auto-adopt** — any tmux session named `claude-*` is picked up automatically, with no explicit "create" step. Real-time via a tmux hook (`scripts/aria-tmux-hook.conf` → `aria-shell-register --ensure-capture`), backstopped by a poll reconciler (`ShellAdoptWorker`) that re-attaches capture to any session the hook missed.
- A `pipe-pane` capture subprocess streams every line into the `shell_events` collection with ANSI stripping and server-assigned line numbers.
- A **snapshot** worker periodically captures the full pane buffer for rehydration after restarts.
- An **idle notifier** watches for shells stuck at an interactive prompt (`[y/n]`, `Human:`, etc.) and enqueues an alert that the Hermes agent relays over Signal.
- A **memory extraction** worker (per-call timeout + cursor self-heal) feeds accumulated events through the memory extractor so long-running coding sessions become searchable facts.
- A **prune** worker enforces per-shell scrollback retention by token budget; a **selfcheck** worker monitors DB/LLM/embeddings/extraction health and raises alerts; a weekly **report** worker texts an "all good" heartbeat (via Hermes) so silence is never ambiguous.
- A **project harvester** derives the project registry from git repos + Claude/pi sessions + live shells.
- The orchestrator injects a recent-activity summary into every chat so ARIA can reference "your coding agent in proj" without asking.

**Using it:**

```bash
# Enable the tmux hook once
tmux source-file scripts/aria-tmux-hook.conf

# Start a watched session — any name prefixed with claude-
tmux new -s claude-myproject

# Or via CLI
aria shells list                    # list registered shells
aria shells info claude-myproject
aria shells tail claude-myproject --lines 50
aria shells send claude-myproject "yes"
aria shells send claude-myproject "C-c" --no-enter
aria shells search "compilation error"
aria shells tags claude-myproject primary urgent
```

**Dashboard tab:** `http://localhost:3000/dashboard/shells` — sidebar list, live scrollback via SSE, special-key palette (Enter/Esc/Ctrl-C/Ctrl-D/↑/↓/yes/no), and a send-input form.

**From chat:** ARIA has a `send_shell_input` tool, so you can ask her to "tell my coding agent yes" or "send Ctrl-C to claude-myproject" in any conversation.

All of this is gated by `SHELLS_ENABLED` in `.env` and disabled cleanly if tmux isn't available.

## MCP Bridge (Hermes)

ARIA exposes its `/api/v1` surface as an **MCP server** (`mcp/server.py`, FastMCP) so the remote **Hermes** agent can drive *all of ARIA* — ~31 tools: the fleet (`fleet_status`, `send_shell_input`, …), **chat with the ARIA orchestrator** + conversations + agents, **memory** (`search_memory`/`add_memory`), **coding sub-agents** (create/drive/stop), projects/tasks (`/todos` + `/projects/{id|slug}`), and alerts.

### Self-healing alerts
Because Hermes owns the single signal-cli daemon, ARIA no longer sends Signal/Telegram itself — it enqueues actionable, cooldown-gated alerts into the `alerts` collection (`selfcheck` alerts once per state-transition; job-lifecycle events are filtered out). Hermes owns the resolution loop (a cron job): on each alert it **spins up a diagnostic coding sub-agent** via the MCP, collects a root-cause + proposed fix, relays *that* to Signal ("reply APPLY…"), and acks. On `APPLY`, Hermes spawns a fixer agent to apply it — verified end-to-end. So ARIA's alerting is fully observable and self-remediating, not just forwarded.

## Background Processes

These are autonomous tasks where ARIA delegates work to a Claude Code CLI instance via the `ClaudeRunner`. They use your Claude subscription (not API tokens) and run without interactive permissions.

| Process | Schedule | What It Does |
|---------|----------|--------------|
| **Dream Cycle** | Every 6h, quiet hours (1am-5am) | Reviews memories, finds patterns, writes journal entries, proposes identity evolution |
| **Research** | On demand (`/research query`) | Recursive web research with branching queries, learning extraction, report synthesis |
| **Heartbeat** | Every 30min, active hours (9am-10pm) | Reviews checklist, alerts via Signal/Telegram if anything needs attention |
| **OODA Loop** | During responses (if enabled) | Scores ARIA's own response quality (0-1), retries if below threshold |
| **Autopilot** | On demand via API | Decomposes goals into steps, executes sequentially with optional approval gates |
| **Awareness** | Every 30min (if enabled) | Monitors git activity, system health, filesystem changes, produces situational summary |
| **Summarization** | Auto when context grows | Rolling conversation compaction preserving goals, decisions, progress, open questions |
| **Memory Extraction** | After conversations | Extracts facts, relationships, events, beliefs with categories and confidence scores |
| **Session Digest** | After coding sessions | Analyzes completed sessions, extracts takeaways, decisions, open questions |

## Safety & Reliability

ARIA's long-running agents are supervised by a layer of safety subsystems inspired by Gas Town's agent-ops patterns:

| Subsystem | Purpose |
|-----------|---------|
| **Context Budget Guard** | Watches coding sessions for context-window exhaustion via heuristic signals (provider limit messages, Claude Code compaction notices, latency spikes). WARN @ 75%, SOFT checkpoint @ 85%, HARD stop @ 92%. |
| **Session Checkpoints** | Persists task, modified files, branch, last commit, and notes to MongoDB so crashed agents can be resumed with full context. |
| **Emergency Stop (Estop)** | MongoDB-backed global freeze that halts all agent activity on API rate limits or critical errors. Visible across processes, auto-thaws when clear. |
| **Inter-Agent Mail** | Structured `TASK_DONE` / `HANDOFF` / `RESULT` / `ERROR` / `CHECKPOINT` messages routed through MongoDB between the orchestrator and sub-agents. |
| **Tmux Backend** | Optional visible backend that spawns each coding agent in its own color-coded tmux pane inside an `aria-agents` session. |
| **Escalation Protocol** | Severity-routed notifications (CRITICAL/HIGH/MEDIUM/LOW) with auto-resolution attempts and auto-re-escalation of stale items. |

## Interfaces

| Interface | Technology | Description |
|-----------|-----------|-------------|
| **TUI** | Go (Bubble Tea) | 4-quadrant terminal dashboard with sidebar, session detail, tools, vitals |
| **Web UI** | Next.js | Chat interface with mode switching and conversation management |
| **Desktop Widget** | Tauri v2 | System tray app, `Ctrl+Space` hotkey, voice input/output |
| **CLI** | Python | `aria chat`, `aria research`, `aria memories search`, `aria tools list` (honors `ARIA_API_URL`) |
| **REST API** | FastAPI | Full API with SSE streaming at `localhost:8200` (the single always-on service) |
| **MCP** | FastMCP (stdio) | `mcp/server.py` — ~31 tools (fleet, chat, memory, coding sub-agents, projects/tasks, alerts), consumed by the Hermes agent |

Each interface maintains its own conversation with ARIA, but all share the same sub-agents, background processes, and long-term memory. Outbound notifications go through the MCP alert queue relayed by Hermes rather than ARIA sending Signal/Telegram directly.

## Memory System

Two-tier architecture with shared access across all interfaces:

**Short-term**: Recent conversation messages per interface. Fast MongoDB queries, token-aware truncation.

**Long-term**: Hybrid search combining:
- `$vectorSearch` — 1024-dim voyage-4-nano embeddings, cosine similarity
- `$search` — BM25 lexical search
- RRF fusion (k=60) merges both result sets

Memories have content types (fact/preference/experience/relationship), categories, confidence scores (with time decay), importance ratings, and access counts.

## LLM Backends

| Backend | Type | Config / models |
|---------|------|--------|
| **Fireworks** | Cloud | `FIREWORKS_API_KEY` — **GLM 5.2** (`glm-5p2`); the default model for the ARIA orchestrator + Pi Coding Agent |
| **llama.cpp** | Local (ROCm) | Qwen3.6 **35B-A3B** `:8092` (`llamacpp_url`) and **27B** `:8093` — AMD Strix Halo GPU box |
| **context-1** | Local (ROCm) | chromadb/context-1 20B `:8081` — the Search Agent's agentic backend |
| **Anthropic** | Cloud | `ANTHROPIC_API_KEY` in `.env` |
| **OpenAI** | Cloud | `OPENAI_API_KEY` in `.env` |
| **OpenRouter** | Cloud (multi) | `OPENROUTER_API_KEY` in `.env` |

All backends implement the same adapter interface; **backend + model are chosen per agent** (config rows in `db.agents`). The local models run as Docker containers in `infrastructure/qwen-rocmfp4/`. The LLM manager handles selection and fallback chains. (The old single `llama.cpp` on `:8080` is retired.)

## Tools

Built-in tools ARIA can call during conversations:

| Tool | Description |
|------|-------------|
| `shell` | Execute shell commands |
| `filesystem` | Read, write, list, delete files |
| `web_fetch` | HTTP requests |
| `screenshot` | Capture and analyze screen |
| `start_coding_session` | Spawn Claude Code or Codex |
| `stop_coding_session` | Stop a running session |
| `get_coding_output` | Read session output |
| `send_to_coding_session` | Send input to running session |
| `send_shell_input` | Send keystrokes to a watched tmux shell |
| `claude_agent` | Delegate a one-shot task to Claude Code |
| `pi_coding_agent` | Delegate to Pi Coding Agent (local LLM) |
| `update_soul` | Read or modify SOUL.md |
| `document_generation` | Generate documentation |

Additionally, MCP servers provide dynamic tools via JSON-RPC 2.0, and the skill system allows installing packaged tool bundles.

## Voice Services

### Text-to-Speech (TTS)

Qwen3-TTS 0.6B CustomVoice on CPU. 9 speakers (Vivian, Serena, Dylan, Eric, Ryan, Aiden, etc.).

- **Service**: `http://localhost:8002`

### Speech-to-Text (STT)

whisper-large-v3-turbo via faster-whisper, CPU with int8 quantization. Auto language detection.

- **Service**: `http://localhost:8003`

## Embedding Service

Local sentence-transformers running `voyageai/voyage-4-nano` on CPU. OpenAI-compatible `/v1/embeddings` endpoint.

- **Model**: voyage-4-nano (MRL truncated to 1024 dims)
- **Service**: `http://localhost:8001`
- **Fallback**: Voyage AI cloud API (if `VOYAGE_API_KEY` is set)

## Quick Start

See **[GETTING_STARTED.md](GETTING_STARTED.md)** for the full setup guide.

```bash
# 1. Clone and configure
git clone https://github.com/baf509/ProjectAria.git
cd ProjectAria
cp .env.example .env        # Edit with your API keys

# 2. Start shared infrastructure (MongoDB + mongot + embeddings)
cd ../infrastructure && docker compose up -d
# Local LLMs (qwen-chat/agentic + context-1) run from infrastructure/qwen-rocmfp4/
cd qwen-rocmfp4 && docker compose up -d && cd ..

# 3. Start ARIA API (native service for filesystem/process access)
systemctl --user start aria-api

# 4. Start ARIA Docker services (TTS, STT, Web UI)
cd ../ProjectAria && docker compose up -d

# 5. Access ARIA
open http://localhost:3000      # Web UI
aria-tui                        # Terminal dashboard
aria chat "Hello, ARIA!"        # CLI
```

## Directory Structure

```
ProjectAria/
├── api/                        # FastAPI backend
│   └── aria/
│       ├── core/               # Orchestrator, context builder, ClaudeRunner, OODA, steering
│       ├── llm/                # LLM adapters (llamacpp, context1, anthropic, openai, openrouter, fireworks)
│       ├── memory/             # Short-term + long-term memory, embeddings, extraction
│       ├── tools/              # Built-in tools + MCP integration
│       ├── agents/             # Coding session manager, watchdog, tmux backend, budget guard, checkpoint, estop, mail
│       ├── dreams/             # Dream cycle service
│       ├── research/           # Recursive web research
│       ├── heartbeat/          # Periodic check-in service
│       ├── autopilot/          # Goal decomposition and execution
│       ├── awareness/          # Environmental sensors (git, filesystem, system, sessions)
│       ├── shells/             # Watched tmux fleet: auto-adopt, capture, snapshot, extraction, prune, selfcheck, report, project harvest
│       ├── planning/           # Projects (harvested + LLM-extracted) and to-do tasks
│       ├── signal/             # Signal bot integration (inbound chat)
│       ├── telegram/           # Telegram bot integration (inbound chat)
│       ├── notifications/      # Alert queue (relayed by Hermes over MCP)
│       ├── workflows/          # Multi-step workflow engine
│       ├── tasks/              # Background task runner
│       ├── skills/             # Skill registry and loader
│       └── db/                 # MongoDB models, migrations, usage tracking
├── mcp/                        # MCP server (FastMCP) exposed to the Hermes agent
├── scripts/                    # tmux hook + aria-shell-capture/register shims
├── tui/                        # Go TUI (Bubble Tea) — 4-quadrant dashboard
├── ui/                         # Next.js web UI
├── widget/                     # Tauri v2 desktop widget
├── cli/                        # Python CLI client
├── tts/                        # TTS microservice (Qwen3-TTS)
├── stt/                        # STT microservice (whisper-large-v3-turbo)
├── docker-compose.yml          # ARIA Docker services (tts, stt, ui)
├── ARCHITECTURE.md             # Detailed architecture documentation
├── GETTING_STARTED.md          # Setup guide
├── SPECIFICATION.md            # Detailed requirements
└── PROJECT_STATUS.md           # Current progress
```

## Development

```bash
# API (with hot-reload; stop the systemd service first or use a spare port — the
# live service binds :8200)
cd api && uvicorn aria.main:app --reload --host 0.0.0.0 --port 8200

# TUI
cd tui && go install . && aria-tui

# Web UI (with hot-reload)
cd ui && npm run dev

# Desktop Widget
cd widget && npm install && npm run tauri:dev

# CLI
cd cli && pip install -e .

# Tests
cd api && python3 -m pytest tests/ -v
```

## Documentation

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — How ARIA thinks, delegates, and gets things done
- **[GETTING_STARTED.md](GETTING_STARTED.md)** — Full setup and usage guide
- **[SPECIFICATION.md](SPECIFICATION.md)** — Detailed architecture and requirements
- **[PROJECT_STATUS.md](PROJECT_STATUS.md)** — Current phase and progress
- **[CHANGELOG.md](CHANGELOG.md)** — Change history

## Key Design Decisions

1. **No framework dependencies** — No LangChain, LlamaIndex, etc. Direct API integration only.
2. **LLM agnostic** — Adapter pattern makes backends swappable with automatic fallback.
3. **MongoDB 8.2 + mongot** — Community Server with vector search, no Atlas needed.
4. **Local-first** — Local LLMs primary, cloud APIs as fallback.
5. **Hybrid search** — BM25 + vector with RRF fusion for memory retrieval.
6. **Sub-agents persist** — Coding sessions stay alive until explicitly stopped, no timeouts.
7. **Background autonomy** — ClaudeRunner delegates to Claude Code CLI using subscription tokens, not API keys.
8. **Identity evolution** — ARIA can propose changes to her own SOUL.md, but changes require user approval.
