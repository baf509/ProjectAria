# ARIA - Autonomous Reasoning & Intelligence Architecture

> Personal AI agent with persistent memory, autonomous sub-agents, background processes, and multiple interfaces — runs on your hardware.

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

A local LLM coding assistant running on llama.cpp. Free, private, always available. ARIA creates a persistent conversation and processes it through her own orchestrator using the local model.

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

## Interfaces

| Interface | Technology | Description |
|-----------|-----------|-------------|
| **TUI** | Go (Bubble Tea) | 4-quadrant terminal dashboard with sidebar, session detail, tools, vitals |
| **Web UI** | Next.js | Chat interface with mode switching and conversation management |
| **Desktop Widget** | Tauri v2 | System tray app, `Ctrl+Space` hotkey, voice input/output |
| **CLI** | Python | `aria chat`, `aria research`, `aria memories search`, `aria tools list` |
| **REST API** | FastAPI | Full API with SSE streaming at localhost:8000 |
| **Signal** | Signal REST API | Chat with ARIA from Signal, including voice message transcription |
| **Telegram** | Telegram Bot API | Chat with ARIA from Telegram with allowlist enforcement |

Each interface maintains its own conversation with ARIA, but all share the same sub-agents, background processes, and long-term memory.

## Memory System

Two-tier architecture with shared access across all interfaces:

**Short-term**: Recent conversation messages per interface. Fast MongoDB queries, token-aware truncation.

**Long-term**: Hybrid search combining:
- `$vectorSearch` — 1024-dim voyage-4-nano embeddings, cosine similarity
- `$search` — BM25 lexical search
- RRF fusion (k=60) merges both result sets

Memories have content types (fact/preference/experience/relationship), categories, confidence scores (with time decay), importance ratings, and access counts.

## LLM Backends

| Backend | Type | Config |
|---------|------|--------|
| **llama.cpp** | Local (ROCm) | AMD APU/GPU acceleration via [lemonade-sdk/llamacpp-rocm](https://github.com/lemonade-sdk/llamacpp-rocm) |
| **Anthropic** | Cloud | `ANTHROPIC_API_KEY` in `.env` |
| **OpenAI** | Cloud | `OPENAI_API_KEY` in `.env` |
| **OpenRouter** | Cloud (multi) | `OPENROUTER_API_KEY` in `.env` |

All backends implement the same adapter interface. The LLM manager handles backend selection and automatic fallback chains.

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

# 2. Start shared infrastructure (MongoDB, llama.cpp, embeddings)
cd ../infrastructure && docker compose up -d

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
│       ├── llm/                # LLM adapters (llamacpp, anthropic, openai, openrouter)
│       ├── memory/             # Short-term + long-term memory, embeddings, extraction
│       ├── tools/              # Built-in tools + MCP integration
│       ├── agents/             # Coding session manager, subprocess manager, watchdog, backends
│       ├── dreams/             # Dream cycle service
│       ├── research/           # Recursive web research
│       ├── heartbeat/          # Periodic check-in service
│       ├── autopilot/          # Goal decomposition and execution
│       ├── awareness/          # Environmental sensors (git, filesystem, system, sessions)
│       ├── signal/             # Signal bot integration
│       ├── telegram/           # Telegram bot integration
│       ├── notifications/      # Cross-channel notification service
│       ├── workflows/          # Multi-step workflow engine
│       ├── tasks/              # Task runner
│       ├── skills/             # Skill registry and loader
│       └── db/                 # MongoDB models, migrations, usage tracking
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
# API (with hot-reload)
cd api && uvicorn aria.main:app --reload --host 0.0.0.0 --port 8000

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
