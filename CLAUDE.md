# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Start for New Sessions

**Always start by reading these files in order:**
1. `PROJECT_STATUS.md` - Current phase and checklist
2. `CHANGELOG.md` (last 50 lines) - Recent changes
3. `SPECIFICATION.md` - Detailed architecture and requirements

## Architecture Overview

ARIA is a local-first AI agent platform — a personal AI assistant with long-term memory, tool use, and multiple interfaces.

ARIA is the **single always-on service** on this host (`corsair-ai`). It listens on
**:8200** and has absorbed the former standalone `aria-shells` service — the
watched-shells / fleet subsystem now lives here (see *Watched Shells & Fleet*
below). It also exposes an **MCP server** (`mcp/server.py`) consumed by the
remote **Hermes** agent. The `aria-shells` repo is retained only as reference.

**Key principles:**
- **Linux service only** — ARIA runs exclusively as a service on a Linux machine. There is no native mobile/iOS client; access is via the Web UI, TUI, CLI, desktop widget, and the REST API, plus the MCP server for the Hermes agent.
- **No framework dependencies** — No LangChain, LlamaIndex, LangGraph, or AutoGen. Direct API integration only.
- **Single-user design** — Personal agent, no multi-tenancy or auth.
- **LLM agnostic** — Adapter pattern for llama.cpp (local), Anthropic, OpenAI, and OpenRouter.
- **Local-first** — llama.cpp primary, cloud APIs as fallback.
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

Adapters: `llamacpp.py`, `anthropic.py`, `openai.py`, `openrouter.py`. The OpenRouter adapter uses the OpenAI SDK internally (OpenAI-compatible API). Manager (`manager.py`) handles backend selection and fallback chain.

### Tool System

- **Built-in tools**: filesystem, shell, web (`api/aria/tools/builtin/`)
- **MCP integration**: stdio transport only, JSON-RPC 2.0 (`api/aria/tools/mcp/`)
- **Tool router**: Central registration, execution with 30s default timeout
- Orchestrator handles tool calls during LLM streaming, may trigger multiple rounds

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
launched by Hermes from `~/.hermes/config.yaml`). Tools wrap `/api/v1`: the fleet
(fleet_status, get_shell_screen, send_shell_input, …), projects/tasks (targeting
native `/todos` + `/projects/{id|slug}`), and the alert relay. After editing
`mcp/server.py`, restart `hermes-gateway.service` to reload the toolset.

### Notifications & Alerts (`api/aria/notifications/`)

ProjectAria does **not** send Signal/Telegram itself (that collided with the
single signal-cli daemon owned by Hermes). `NotificationService.notify()`
enqueues cooldown-gated alerts into the `alerts` collection; Hermes pulls them
over MCP (`list_alerts`/`ack_alert`) and relays via its own Signal. Routes:
`/api/v1/alerts`.

## Shared Infrastructure

ARIA depends on shared infrastructure at `/home/ben/Development/infrastructure/` (also used by AgentBenchPlatform). **Must be started first.**

| Service | Port | Purpose |
|---------|------|---------|
| mongod | 27017 | MongoDB 8.2 data (replica set `rs0`) |
| mongot | 27028 | MongoDB search (vector + text) |
| llamacpp | 8080 | ROCm-accelerated local LLM |
| embeddings | 8001 | voyage-4-nano via sentence-transformers (CPU) |

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
```

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

## Critical Gotchas

### Embedding Dimensions (DO NOT CHANGE)

Model is `voyageai/voyage-4-nano` with **1024-dim MRL truncation**. The MongoDB vector index, embedding service, and all stored memories must use exactly 1024 dimensions. Changing requires full re-embedding of all memories.

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

Test suite covers: tokenizer, resilience (retry/circuit breaker), tool router (registration, policy, execution, audit), LLM base classes, memory RRF fusion, orchestrator command parsing (mode/research/memory/coding), research service (JSON parsing, deduplication, HTML stripping), and workflow engine (conditions, dependencies, parameter interpolation).

Additional manual testing via CLI, API docs (`/docs`), and Docker Compose integration.
