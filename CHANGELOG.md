# ARIA Changelog

All notable changes to ARIA will be documented in this file.

Format:
```
## [Date] - Phase X - [Summary]

### Added
- New features

### Changed
- Changes to existing features

### Fixed
- Bug fixes

### Removed
- Removed features

### Notes
- Important notes for future work
```

## [2026-04-18] - Native iOS / iPadOS client + shells/devices API

### Added
- **`ios/` subfolder** — native SwiftUI app targeting iOS 17+, Swift 6, Xcode 26.
  Project is generated via XcodeGen (`ios/project.yml`). See `ios/README.md`.
  - **AriaKit** local SPM package: `Sendable` Codable models, `URLSession`-based
    `AriaClient` with optional `X-API-Key`, `AsyncThrowingStream` SSE parser,
    Keychain helper. Typed API clients: Shells, Conversations, Memories,
    Health, Devices.
  - **AriaMobile** app target: TabView root on iPhone, `NavigationSplitView`
    on iPad. Full shells coverage — list/filter/search, create sheet, SwiftTerm
    live ANSI terminal on detail with 2000-event backfill + SSE tail + auto
    reconnect, input bar + key accessory bar (Esc/Tab/arrows/⏎/Ctrl-?/yes/no),
    kill session, edit tags, 3s snapshot view, noise filter toggle. Also
    chat with streaming SSE replies + steer, memory search (debounced
    hybrid search).
  - **SwiftTerm** SPM dependency for real VT100 rendering from day one.
- **`POST /api/v1/shells`** — create a detached tmux session and register it
  as a watched shell. Body: `{name, workdir?, launch_claude=true}`. Name is
  prefixed with the configured shells prefix (`claude-`) if not already.
  Launches Claude Code in the new session by default.
- **`DELETE /api/v1/shells/{name}`** — kill a tmux session and mark its
  shell row stopped.
- `ShellService.create_shell` / `kill_shell` and `TmuxClient.new_session`
  wrap these operations; existing tmux hooks (`session-created`) continue
  to wire up pipe-pane capture for new sessions.
- **`POST /api/v1/devices`** / **`DELETE /api/v1/devices/{token}`** — APNs
  device-token registration for mobile push. Stored in `devices` collection.
- **APNs idle alerts** (feature-flagged) — `IdleNotifier` now fans out to
  `send_apns_alert` when `shells_apns_enabled=true`. Transport itself is a
  stub (`api/aria/shells/apns.py`) with clear config-check + logging; flip
  the flag and drop in `httpx[http2]`/`aioapns` when ready to deliver.

### Changed
- `api/aria/main.py` registers the new `devices` router.
- `api/aria/config.py` gains `shells_apns_enabled`, `apns_team_id`,
  `apns_key_id`, `apns_bundle_id`, `apns_auth_key_path`, `apns_use_sandbox`.

### Notes
- No change needed to the existing shells SSE payload: `ShellEvent` was
  already serialized via `model_dump_json()` which includes `text_raw`, so
  SwiftTerm gets raw ANSI from the existing `/shells/{name}/stream` route.
- Route tests: `tests/test_shells_routes.py` (+6 cases) and
  `tests/test_devices_routes.py` (+5 cases), all green.

## [2026-04-15] - Local Agentic Search (Chroma context-1)

### Added
- **context-1 LLM backend** (`api/aria/llm/context1.py`) — new "context1" backend
  registered in `llm/manager.py`, pointing at a second llama.cpp instance that
  serves the chromadb/context-1 20B GGUF
  (https://huggingface.co/ryancook/chromadb-context-1-gguf). Configured via
  `context1_url`, `context1_model`, `context1_max_iterations`, `context1_max_docs`,
  and `context1_fs_allowed_roots` in `config.py`.
- **Search Agent tool** (`api/aria/tools/builtin/search_agent.py`) — agentic
  observe/reason/act retrieval loop driven by context-1. Exposes six tools to
  the model: `memory_search` (hybrid vector+BM25 over `memories`), `web_search`
  + `web_read` (existing search provider + WebTool), `fs_grep` + `fs_read`
  (ripgrep + bounded file reads over allowed roots), `prune`, and `finalize`.
  Returns a ranked list of documents with stable `mem:`/`web:`/`file:` ids.
  Registered at startup when the context1 backend is available.
- **Search Agent profile** (`slug=search-agent`, seeded in `db/migrations.py`) —
  named agent profile with `mode_category=research`, `backend=context1`, and
  `enabled_tools=[search_agent, web, filesystem, deep_think]`.
- **Research service integration** (`api/aria/research/service.py`) — when the
  research run's backend is `context1`, the branch loop now gathers sources via
  `search_agent` (over memory + web + local files) instead of the web-only
  provider path. Falls back to the web provider if the tool errors.
- **Infrastructure service** (`infrastructure/docker-compose.yml`) — new
  `llamacpp-context1` service (profile `context1`) on port 8081 that reuses the
  existing ROCm llama.cpp build to serve the context-1 GGUF from
  `${LLAMACPP_MODELS_DIR}/context-1/`.

### Notes
- The GGUF must be downloaded once into `infrastructure/models/llm/context-1/`
  (e.g. `chromadb-context-1-Q4_K_M.gguf`). Start the service with
  `docker compose --profile context1 up -d llamacpp-context1`.
- The model does not have upstream tool-calling templates documented; the
  service launches with `--jinja` so llama.cpp's OpenAI-compatible tool-call
  path is active. Adjust `CONTEXT1_ARGS` if the template needs tuning.

## [2026-04-13] - Agent Safety Subsystems & Escalation

### Added
- **Context Budget Guard** (`api/aria/agents/budget_guard.py`) — monitors coding
  sessions for context-window exhaustion via heuristic signals (provider limit
  messages, Claude Code compaction notices, latency spikes, explicit mentions).
  Thresholds: WARN 75%, SOFT 85% (checkpoint + notify), HARD 92% (checkpoint +
  stop + suggest resume).
- **Session Checkpointing** (`api/aria/agents/checkpoint.py`) — persists coding
  session state (current task, modified files, branch, last commit, notes) to
  MongoDB so crashed agents can be resumed with full context.
- **Emergency Stop / Rate-Limit Watchdog** (`api/aria/agents/estop.py`) —
  MongoDB-backed global estop that freezes all agent activity on API rate
  limits or critical errors. Visible across processes, persists across restarts,
  auto-thaws when clear.
- **Inter-Agent Mail Protocol** (`api/aria/agents/mail.py`) — structured
  agent-to-agent messages (`TASK_DONE`, `HANDOFF`, `RESULT`, `ERROR`,
  `CHECKPOINT`) stored in MongoDB and polled by the orchestrator.
- **Tmux Agent Backend** (`api/aria/agents/backends/tmux.py`) — spawns coding
  agents in visible, color-coded tmux panes in a dedicated `aria-agents`
  session so the user can watch multiple agents work in parallel.
- **Escalation Protocol** (`api/aria/notifications/escalation.py`) — severity
  routing (CRITICAL/HIGH/MEDIUM/LOW) with auto-resolution attempts before user
  notification and auto-re-escalation of stale items.
- Broad test coverage across new and existing subsystems: agent mail, autopilot,
  awareness, budget guard, builtin tools, checkpoint, coding session, context
  builder, db models, dream service, embeddings, escalation, estop, killswitch,
  llm manager, mcp, orchestrator, orchestrator tool loop, short-term memory,
  steering, usage repo, watchdog.

### Changed
- Expanded `agents/watchdog.py` and `agents/session.py` to integrate budget
  guard, checkpoint, estop, and mail signals.
- Extended `dreams/service.py` and `prompts/dream_reflection.md` with deeper
  reflection flow.
- Wired new safety dependencies through `api/deps.py`, `main.py`, and DB
  migrations (`db/migrations.py`).

### Notes
- Inspired by Gas Town's context-budget-guard, checkpoint, rate-limit-watchdog,
  mail, and tiered escalation patterns.

---

## [2026-02-16] - Phase 6 - Voice I/O (TTS + STT)

### Added
- TTS microservice (`tts/`) running Qwen3-TTS 0.6B CustomVoice model on CPU
- `POST /v1/tts/synthesize` endpoint for speech synthesis (returns WAV audio)
- `GET /v1/tts/speakers` endpoint listing 9 available voice speakers
- `GET /v1/tts/health` endpoint for TTS service health checks
- API proxy routes forwarding TTS requests to the microservice
- Widget play button on assistant messages for reading responses aloud
- Docker Compose `tts` service on port 8002
- `TTS_URL` configuration in `.env.example` and API settings
- STT microservice (`stt/`) running whisper-large-v3-turbo via faster-whisper on CPU (int8)
- `POST /v1/stt/transcribe` endpoint accepting audio file upload, returns transcribed text
- `GET /v1/stt/health` endpoint for STT service health checks
- API proxy routes forwarding STT requests to the microservice
- Widget mic button now functional: click to record, click again to stop and transcribe
- Recording indicator (pulsing red) and transcribing state on mic button
- Docker Compose `stt` service on port 8003
- `STT_URL` configuration in `.env.example` and API settings

---

## [2025-12-28] - Phase 2 - Fix Memory Extraction Background Tasks

### Fixed
- **Memory extraction now uses FastAPI BackgroundTasks instead of asyncio.create_task()**
  - Orchestrator now accepts `background_tasks` parameter from API routes
  - Proper lifecycle management for background memory extraction tasks
  - Prevents memory extraction tasks from being cancelled prematurely
  - Fallback to asyncio.create_task for non-HTTP contexts (CLI, tests)
  - Location: `api/aria/core/orchestrator.py:280-309`

- **Manual memory extraction API now uses agent's LLM configuration**
  - Fixed `/api/v1/memories/extract/{conversation_id}` endpoint
  - Previously used hardcoded defaults (ollama/llama3.2:latest)
  - Now correctly looks up and uses the agent's configured LLM backend and model
  - Ensures extraction works with OpenRouter, Anthropic, OpenAI, etc.
  - Location: `api/aria/api/routes/memories.py:232-251`

- **Message sending routes now pass BackgroundTasks to orchestrator**
  - Updated `/api/v1/conversations/{id}/messages` endpoint
  - Passes BackgroundTasks to orchestrator for proper memory extraction scheduling
  - Works for both streaming and non-streaming modes
  - Location: `api/aria/api/routes/conversations.py:154-196`

### Changed
- Added logging to memory extraction with `[MEMORY EXTRACTION]` prefix for easier debugging
- Memory extraction errors now print to logs for troubleshooting

### Notes
- **Action Required**: Restart API container to apply these fixes
- These fixes resolve the issue where memories were not being automatically extracted from conversations
- Memory extraction will now work reliably with any LLM backend (Ollama, OpenRouter, Anthropic, OpenAI)

---

## [2025-12-27] - Phase 5 - Fix Missing UI API Client

### Fixed
- Created missing `ui/src/lib/api-client.ts` file
  - Implements complete API client for ARIA API
  - Methods for health check, conversations, agents, memories, tools, MCP
  - Streaming message support with Server-Sent Events
  - TypeScript types integration
- Resolves UI build errors ("Module not found: Can't resolve '@/lib/api-client'")

### Added
- Complete API client implementation with:
  - Health check endpoint
  - Conversation CRUD and streaming
  - Agent management
  - Memory operations and search
  - Tool listing and execution
  - MCP server management

### Notes
- This file was referenced in UI code but was missing from the repository
- Phase 5 Web UI can now build successfully

---

## [2025-12-27] - Infrastructure - MongoDB Community Search Update

### Changed
- Updated MongoDB Community Search (mongot) to version 0.55.0 (from 0.53.1)
  - `docker-compose.yml` - Updated mongot image version
  - `SPECIFICATION.md` - Updated documentation to reflect latest version
- Latest mongot version provides improved performance and bug fixes

### Notes
- Version 0.55.0 is the latest stable release of MongoDB Community Search
- No breaking changes from 0.53.1 to 0.55.0
- Existing mongot data volumes remain compatible

---

## [2025-12-27] - Phase 4 - OpenRouter Health Check Fix

### Fixed
- Added OpenRouter to health check endpoint (`api/aria/api/routes/health.py`)
  - OpenRouter now included in `/api/v1/health/llm` status checks
  - Completes OpenRouter integration (was missing from health check backends list)

---

## [2025-12-25] - Phase 4 - OpenRouter Support

### Added
- OpenRouter adapter (`api/aria/llm/openrouter.py`)
  - OpenAI-compatible API for unified access to multiple LLM providers
  - Supports models from OpenAI, Anthropic, Google, Meta, and more
  - Streaming support with proper message formatting
  - Tool use (function calling) support
  - Optional HTTP-Referer and X-Title headers for app rankings
  - Uses OpenAI SDK with custom base URL (https://openrouter.ai/api/v1)
- OpenRouter configuration
  - `OPENROUTER_API_KEY` environment variable
  - Added to `.env.example` with other API keys
  - Configuration in `api/aria/config.py`
  - Docker compose environment variable pass-through

### Changed
- Updated LLM manager (`api/aria/llm/manager.py`)
  - Added "openrouter" backend support
  - Backend availability check for OpenRouter
  - Error messages for missing OpenRouter API key
- Updated documentation
  - `CLAUDE.md` - Added OpenRouter to adapter list and configuration
  - `SPECIFICATION.md` - Added OpenRouter to cloud API options
  - `README.md` - Updated LLM agnostic description
  - Added API key security section to CLAUDE.md

### Notes
- OpenRouter reuses the OpenAI SDK (no additional dependencies)
- Model names in OpenRouter use provider prefixes (e.g., "openai/gpt-4")
- API keys stored in `.env` file (git-ignored for security)
- OpenRouter provides cost-effective access to multiple providers through one API

---

## [2025-12-25] - Documentation & Configuration - CLAUDE.md and Embedding Standardization

### Added
- `CLAUDE.md` - Comprehensive guide for Claude Code sessions
  - Quick start instructions (PROJECT_STATUS.md, CHANGELOG.md, SPECIFICATION.md)
  - Architecture overview (core flow, memory system, tools, LLM adapters)
  - MongoDB 8.2 + mongot setup details
  - Development commands (Docker, API, UI, CLI, Database)
  - Code patterns and conventions
  - Key files reference organized by layer
  - Configuration examples
  - Important gotchas and best practices
  - Current phase summary

### Changed
- **Embedding configuration standardized across entire codebase**
  - Model: Changed from `qwen3:8b` to `qwen3-embedding:0.6b`
  - Dimensions: Changed from 4096 to 1024
  - Updated files:
    - `.env.example` - Default embedding model and dimensions
    - `api/aria/config.py` - Configuration class defaults
    - `SPECIFICATION.md` - All architecture diagrams, code examples, and index definitions
    - `README.md` - Key design decisions
    - `CLAUDE.md` - All embedding references
- Improved clarity in embedding configuration
  - Comment: "Using qwen3-embedding:0.6b model with 1024-dimensional embeddings for optimal balance of quality and performance"
  - Note: docker-compose.yml and scripts/init-mongo.js were already correct at 1024 dims

### Fixed
- Configuration inconsistencies between environment files, code defaults, and documentation
- All vector search index definitions now consistently use 1024 dimensions
- All embedding service references now use correct model name

### Notes
- This standardization ensures vector search works correctly with actual embedding dimensions
- MongoDB vector index in `scripts/init-mongo.js` already used 1024 dims
- Docker compose defaults already used `qwen3-embedding:0.6b`
- Changes align runtime configuration with actual deployed setup

---

## [2025-12-06] - Phase 4 - Cloud LLM Adapters

### Added
- Anthropic/Claude adapter (`api/aria/llm/anthropic.py`)
  - Streaming support with proper message formatting
  - Tool use (function calling) support
  - Support for all Claude 3 models (Opus, Sonnet, Haiku)
  - System prompt handling
  - Error handling with proper error messages
- OpenAI adapter (`api/aria/llm/openai.py`)
  - Streaming support with chunk accumulation
  - Function calling support
  - Support for GPT-4, GPT-4 Turbo, GPT-3.5
  - Proper message role handling (including tool role)
  - Error handling
- LLM backend availability check (`api/aria/llm/manager.py`)
  - Check if API keys are configured
  - Verify SDK packages are installed
  - Helpful error messages for missing configuration
- LLM health endpoint (`api/aria/api/routes/health.py`)
  - `GET /api/v1/health/llm` - Check all LLM backend status
  - Returns availability and reason for each backend

### Changed
- Updated LLM manager (`api/aria/llm/manager.py`)
  - Register Anthropic adapter when API key present
  - Register OpenAI adapter when API key present
  - Lazy import of cloud adapters to avoid import errors
  - Validate API keys before creating adapters
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Added `_get_llm_with_fallback()` method
  - Automatic fallback to cloud LLMs on error
  - User notification when fallback is used
  - Support for configurable fallback conditions
- Updated configuration
  - API keys already present in config (ANTHROPIC_API_KEY, OPENAI_API_KEY)
  - Already in `.env.example` file

### Notes
- Cloud LLM packages (anthropic, openai) already in requirements.txt
- API keys must be set in environment variables
- Fallback chain configured per agent in agent.fallback_chain
- Fallback conditions: on_error, on_context_overflow (future)
- Cloud LLMs automatically used when primary LLM fails

---

## [2025-12-06] - Phase 3 - Tools & MCP

### Added
- Tool infrastructure (`api/aria/tools/`)
  - BaseTool abstract class with parameter validation
  - ToolRouter for tool registration and execution
  - ToolDefinition and ToolParameter models
  - ToolResult with status tracking and metadata
- Built-in tools (`api/aria/tools/builtin/`)
  - Filesystem tool: read/write files, list directories, manage files
  - Shell tool: execute commands with timeout and sandboxing
  - Web tool: HTTP GET requests with size limits
- MCP (Model Context Protocol) integration (`api/aria/tools/mcp/`)
  - MCP client with JSON-RPC 2.0 over stdio
  - MCP manager for multi-server lifecycle management
  - MCPToolWrapper for BaseTool compatibility
  - Server health tracking and tool registration
- Tool management API routes (`api/aria/api/routes/tools.py`)
  - List tools (with type filtering)
  - Get tool details
  - Execute tools directly
  - MCP server CRUD (add, remove, list)
  - List tools per MCP server
  - Tool statistics endpoint
- CLI tool commands
  - `aria tools list` - List available tools
  - `aria tools info <name>` - Show tool details
  - `aria tools execute <name> <args>` - Execute a tool
  - `aria mcp list` - List MCP servers
  - `aria mcp add <id> <command>` - Add MCP server
  - `aria mcp remove <id>` - Remove MCP server
  - `aria mcp tools <id>` - List server's tools

### Changed
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Added tool_router parameter
  - Tool definitions passed to LLM when tools enabled
  - Handle tool calls from LLM responses
  - Execute tools and save results to conversation
  - Tool results included in streaming response
- Updated main app (`api/aria/main.py`)
  - Initialize built-in tools on startup
  - Register tools with tool router
  - Shutdown MCP servers on app shutdown
  - Added tools routes
- Updated API dependencies (`api/aria/api/deps.py`)
  - Added get_tool_router() dependency
  - Added get_mcp_manager() dependency
  - Pass tool_router to orchestrator
- Updated CLI with tool and MCP management commands
- Updated `PROJECT_STATUS.md` to Phase 3

### Notes
- Tools must be explicitly enabled per agent (capabilities.tools_enabled)
- Agents can specify which tools they can use (enabled_tools list)
- Filesystem tool is sandboxed to allowed paths (default: user home)
- Shell commands can be filtered with allow/deny lists
- MCP servers communicate via stdio transport
- Tool execution has configurable timeout (default: 5 minutes)

---

## [2025-11-29] - Phase 2 - Memory System

### Added
- Embedding service (`api/aria/memory/embeddings.py`)
  - Ollama embeddings using Qwen3-8b (4096 dimensions)
  - Voyage AI fallback provider
  - Batch embedding support
- Short-term memory (`api/aria/memory/short_term.py`)
  - Current conversation context retrieval
  - Recent conversations context
  - Token budget management
- Long-term memory (`api/aria/memory/long_term.py`)
  - Vector search using MongoDB Atlas Vector Search
  - Lexical search using MongoDB Atlas Search (BM25)
  - Hybrid search with Reciprocal Rank Fusion (RRF)
  - Memory CRUD operations with automatic embedding generation
  - Access tracking and statistics
- Memory extraction pipeline (`api/aria/memory/extraction.py`)
  - LLM-based extraction from conversations
  - Batch message processing
  - Source tracking and confidence scoring
  - Manual extraction from arbitrary text
- Context builder (`api/aria/core/context.py`)
  - Memory injection into system prompts
  - Short-term + long-term memory integration
  - Relevance-based memory retrieval
- Memory API routes (`api/aria/api/routes/memories.py`)
  - List, create, get, update, delete memories
  - Hybrid search endpoint
  - Background extraction trigger
- CLI memory commands
  - `aria memories list` - List all memories
  - `aria memories search` - Search with hybrid search
  - `aria memories add` - Manually add memories
  - `aria memories extract` - Trigger extraction

### Changed
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Integrated context builder for memory-aware responses
  - Automatic memory extraction in background
  - Access tracking for retrieved memories
- Updated main app to include memory routes
- Updated CLI with memory management commands
- Updated `PROJECT_STATUS.md` to Phase 2

### Notes
- Requires MongoDB search indexes for vector and lexical search
- Embeddings require Ollama with embedding model (e.g., qwen3:8b)
- Memory extraction runs asynchronously to avoid blocking chat
- Hybrid search combines best of lexical and semantic search

## [2025-11-29] - Phase 1 - Core Implementation

### Added
- Docker infrastructure (`docker-compose.yml`, Dockerfile, `.env.example`)
- MongoDB initialization script with replica set and search indexes
- FastAPI application foundation
  - Main app with lifespan management (`api/aria/main.py`)
  - Configuration management (`api/aria/config.py`)
  - MongoDB connection layer (`api/aria/db/mongodb.py`)
  - Pydantic models for API (`api/aria/db/models.py`)
- LLM adapter layer
  - Base adapter interface (`api/aria/llm/base.py`)
  - Ollama adapter with streaming support (`api/aria/llm/ollama.py`)
  - LLM manager for backend selection (`api/aria/llm/manager.py`)
- Agent orchestrator (`api/aria/core/orchestrator.py`)
  - Message processing with streaming
  - Conversation context assembly
  - Response persistence
- API routes
  - Health check endpoint
  - Conversations CRUD with SSE streaming
  - Agents CRUD
- CLI client (`cli/aria_cli/main.py`)
  - Interactive chat mode
  - Conversation management commands
  - Agent listing

### Changed
- Updated `PROJECT_STATUS.md` to reflect Phase 1 progress
- All core Phase 1 checklist items marked complete

### Notes
- Phase 1 implementation complete, ready for testing
- Requires Ollama running locally for testing
- MongoDB 8.2 with mongot for vector search
- Testing infrastructure not yet implemented

## [Unreleased]

### Added
- Initial project specification (`SPECIFICATION.md`)
- Project status tracking (`PROJECT_STATUS.md`)
- This changelog

### Changed
- Updated MongoDB configuration to use Community Server 8.2 + mongot
- Switched from `mongodb-atlas-local` to separate `mongod` + `mongot` services
- Updated embedding dimension to 4096 (Qwen3-8b)
- Added hybrid search (BM25 + Vector) with RRF fusion for long-term memory

### Notes
- See `PROJECT_STATUS.md` for current checklist
- MongoDB 8.2 Vector Search is in Public Preview
- mongot image: `mongodb/mongodb-community-search:0.53.1`
- mongod image: `mongodb/mongodb-community-server:8.2.0-ubi9`

---

<!-- 
Template for new entries:

## [YYYY-MM-DD] - Phase X - [Summary]

### Added
- 

### Changed
- 

### Fixed
- 

### Notes
- 

-->
