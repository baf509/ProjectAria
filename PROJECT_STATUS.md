# ARIA Project Status

**Last Updated:** 2026-03-15
**Updated By:** Claude Code

---

## Current Phase

```
╔══════════════════════════════════════════════════════════════════╗
║  MULTI-PHASE: Phases 7-19 in progress                           ║
║  Status: IMPLEMENTATION IN PROGRESS                              ║
╚══════════════════════════════════════════════════════════════════╝
```

---

## Phase 6 Checklist

### llama.cpp ROCm Backend
- [x] llama.cpp adapter (`api/aria/llm/llamacpp.py`)
  - [x] Subclasses OpenAI adapter (OpenAI-compatible API)
  - [x] Configurable base URL and API key
- [x] LLM manager registration (`api/aria/llm/manager.py`)
  - [x] `llamacpp` backend in `get_adapter()`
  - [x] `llamacpp` backend in `is_backend_available()`
- [x] Configuration (`api/aria/config.py`)
  - [x] `LLAMACPP_URL` setting
  - [x] `LLAMACPP_API_KEY` setting
- [x] Docker service (`llamacpp/Dockerfile`)
  - [x] Pre-built ROCm binaries from lemonade-sdk/llamacpp-rocm
  - [x] gfx1151 default target (Ryzen AI MAX+ Pro 395)
  - [x] Configurable GPU target (gfx1150, gfx120X, gfx110X)
  - [x] GPU device passthrough (/dev/kfd, /dev/dri)
  - [x] Model volume mount
- [x] docker-compose.yml integration
  - [x] `llamacpp` service with ROCm device access
  - [x] Environment variable configuration
  - [x] Network connectivity to API service

### Desktop Widget (Tauri v2)
- [x] Project scaffold (`widget/`)
  - [x] Tauri v2 with Vite + TypeScript
  - [x] System tray icon with menu
  - [x] Global hotkey (Ctrl+Space) to toggle window
  - [x] Floating window (400x500, always on top, no decorations)
- [x] Chat UI
  - [x] Message list with streaming updates
  - [x] Text input with Enter to send
  - [x] Auto-resize textarea
  - [x] Escape to hide window
  - [x] New Chat button
- [x] API integration
  - [x] ARIA API client with SSE streaming
  - [x] Conversation auto-creation and resume
  - [x] Settings panel (API URL)
- [x] UI/UX
  - [x] Dark theme (Tokyo Night-inspired)
  - [x] Mic button placeholder (for future voice)
  - [x] Draggable titlebar

### Web UI Fix
- [x] Created missing `ui/src/lib/api-client.ts`
  - [x] `checkHealth()`, `listConversations()`, `getConversation()`
  - [x] `createConversation()`, `streamMessage()` (async generator)
  - [x] SSE streaming support

### Documentation
- [x] Updated README.md
- [x] Updated GETTING_STARTED.md with full setup steps
- [x] Updated PROJECT_STATUS.md
- [x] Updated .env.example with llama.cpp variables

### Future Enhancements
- [ ] Voice input/output (STT/TTS) in widget
- [ ] React Native mobile app
- [ ] Widget settings persistence via Tauri store
- [ ] Configurable hotkey
- [ ] Agent management UI
- [ ] Memory browser/viewer

---

## Phase 5 Checklist

### Project Setup
- [x] Next.js 14 project structure (`ui/`)
  - [x] TypeScript configuration
  - [x] Tailwind CSS setup
  - [x] App Router structure
  - [x] Package.json with dependencies

### Core Components
- [x] API Client (`ui/src/lib/api-client.ts`)
  - [x] Type-safe API methods
  - [x] Streaming support for messages
  - [x] All ARIA endpoints covered
- [x] TypeScript types (`ui/src/types/index.ts`)
  - [x] All API response types
  - [x] Frontend-specific types

### User Interface
- [x] Home page (`ui/src/app/page.tsx`)
  - [x] API health check
  - [x] Auto-redirect to chat
- [x] Chat interface (`ui/src/app/chat/page.tsx`)
  - [x] Real-time streaming responses
  - [x] Message history display
  - [x] Conversation sidebar
  - [x] New conversation creation
  - [x] Responsive design

### Docker Support
- [x] Dockerfile for production build
- [x] Updated docker-compose.yml with UI service
- [x] Environment variable configuration

---

## Phase 4 Checklist

### LLM Adapters
- [x] Anthropic/Claude adapter (`api/aria/llm/anthropic.py`)
  - [x] Streaming support
  - [x] Tool use (function calling)
  - [x] Message format conversion
  - [x] Error handling
- [x] OpenAI adapter (`api/aria/llm/openai.py`)
  - [x] Streaming support
  - [x] Function calling
  - [x] Message format conversion
  - [x] Error handling

### LLM Manager Updates
- [x] Updated LLM manager (`api/aria/llm/manager.py`)
  - [x] Register Anthropic adapter
  - [x] Register OpenAI adapter
  - [x] API key validation
  - [x] Backend availability check
  - [x] Helpful error messages

### Fallback Chain
- [x] Fallback logic in orchestrator (`api/aria/core/orchestrator.py`)
  - [x] Try primary LLM first
  - [x] Automatic fallback on error
  - [x] Configurable fallback conditions
  - [x] User notification of fallback usage

### Configuration
- [x] API key support in config (`api/aria/config.py`)
  - [x] ANTHROPIC_API_KEY
  - [x] OPENAI_API_KEY
- [x] Updated `.env.example` with API keys
- [x] Updated `requirements.txt` with cloud LLM SDKs

### API Endpoints
- [x] `GET /api/v1/health/llm` - Check LLM backend status

### Testing
- [ ] Test Anthropic adapter with Claude
- [ ] Test OpenAI adapter with GPT-4
- [ ] Test fallback chain logic
- [ ] Test API key validation

---

## Phase 3 Checklist

### Tool Infrastructure
- [x] Tool base interface (`api/aria/tools/base.py`)
  - [x] BaseTool abstract class
  - [x] ToolDefinition and ToolParameter models
  - [x] ToolResult with status tracking
  - [x] Parameter validation
- [x] Tool router (`api/aria/tools/router.py`)
  - [x] Tool registration and management
  - [x] Tool execution with timeout
  - [x] Tool definition export for LLMs
  - [x] Error handling and logging

### Built-in Tools
- [x] Filesystem tool (`api/aria/tools/builtin/filesystem.py`)
  - [x] read_file, write_file operations
  - [x] list_directory, create_directory
  - [x] delete_file, file_exists, get_file_info
  - [x] Path validation and sandboxing
- [x] Shell tool (`api/aria/tools/builtin/shell.py`)
  - [x] Command execution with timeout
  - [x] stdout/stderr capture
  - [x] Exit code handling
  - [x] Command filtering (allow/deny lists)
- [x] Web tool (`api/aria/tools/builtin/web.py`)
  - [x] HTTP GET requests
  - [x] Custom headers support
  - [x] Response size limits
  - [x] Timeout configuration

### MCP Integration
- [x] MCP client (`api/aria/tools/mcp/client.py`)
  - [x] JSON-RPC 2.0 protocol implementation
  - [x] stdio transport
  - [x] Server initialization and lifecycle
  - [x] Tool listing and execution
- [x] MCP manager (`api/aria/tools/mcp/manager.py`)
  - [x] Multi-server management
  - [x] Tool registration from MCP servers
  - [x] MCPToolWrapper for BaseTool compatibility
  - [x] Server health tracking

### Orchestrator Integration
- [x] Tool support in orchestrator (`api/aria/core/orchestrator.py`)
  - [x] Tool definitions passed to LLM
  - [x] Tool call handling from LLM responses
  - [x] Tool execution and result collection
  - [x] Tool results saved to conversation

### API Endpoints
- [x] `GET /api/v1/tools` - List all tools
- [x] `GET /api/v1/tools/{tool_name}` - Get tool details
- [x] `POST /api/v1/tools/execute` - Execute a tool
- [x] `GET /api/v1/mcp/servers` - List MCP servers
- [x] `POST /api/v1/mcp/servers` - Add MCP server
- [x] `DELETE /api/v1/mcp/servers/{id}` - Remove MCP server
- [x] `GET /api/v1/mcp/servers/{id}/tools` - List server tools
- [x] `GET /api/v1/tools/stats` - Get tool statistics

### CLI Commands
- [x] `aria tools list` - List available tools
- [x] `aria tools info <name>` - Show tool details
- [x] `aria tools execute <name> <args>` - Execute a tool
- [x] `aria mcp list` - List MCP servers
- [x] `aria mcp add <id> <command>` - Add MCP server
- [x] `aria mcp remove <id>` - Remove MCP server
- [x] `aria mcp tools <id>` - List server's tools

### Testing
- [ ] Test built-in tools (filesystem, shell, web)
- [ ] Test tool execution through orchestrator
- [ ] Test MCP client with sample server
- [ ] Test tool calling in conversations

---

## Phase 2 Checklist

### Memory Infrastructure
- [x] Embedding service (`api/aria/memory/embeddings.py`)
  - [x] Local embeddings via sentence-transformers service (voyage-4-nano)
  - [x] Voyage AI cloud fallback
  - [x] Batch embedding support
- [x] Short-term memory (`api/aria/memory/short_term.py`)
  - [x] Current conversation context
  - [x] Recent conversations context
  - [x] Token budget management
- [x] Long-term memory (`api/aria/memory/long_term.py`)
  - [x] Vector search via mongot
  - [x] Lexical search (BM25) via mongot
  - [x] Reciprocal Rank Fusion (RRF)
  - [x] Memory CRUD operations

### Memory Extraction
- [x] Extraction pipeline (`api/aria/memory/extraction.py`)
  - [x] LLM-based extraction from conversations
  - [x] Batch processing
  - [x] Source tracking
  - [x] Confidence scoring

### Context Integration
- [x] Context builder (`api/aria/core/context.py`)
  - [x] Memory injection into system prompt
  - [x] Short-term + long-term integration
  - [x] Relevance-based memory retrieval
- [x] Orchestrator updated
  - [x] Uses context builder
  - [x] Automatic memory extraction (background)
  - [x] Access tracking

### API Endpoints
- [x] `GET /api/v1/memories` - List memories
- [x] `POST /api/v1/memories` - Create memory
- [x] `GET /api/v1/memories/{id}` - Get memory
- [x] `PATCH /api/v1/memories/{id}` - Update memory
- [x] `DELETE /api/v1/memories/{id}` - Delete memory
- [x] `POST /api/v1/memories/search` - Search memories (hybrid)
- [x] `POST /api/v1/memories/extract/{conversation_id}` - Extract memories

### CLI Commands
- [x] `aria memories list` - List all memories
- [x] `aria memories search <query>` - Search memories
- [x] `aria memories add <content>` - Add memory manually
- [x] `aria memories extract <conversation_id>` - Trigger extraction

### Testing
- [ ] Test embedding generation
- [ ] Test hybrid search
- [ ] Test memory extraction
- [ ] Test context building

---

## Phase 1 Checklist

### Infrastructure
- [x] `docker-compose.yml` created
- [x] MongoDB container configured (mongod + mongot)
- [x] API container configured
- [x] `.env.example` created
- [x] Basic health check working

### API Service
- [x] FastAPI app initialized (`api/aria/main.py`)
- [x] Config module (`api/aria/config.py`)
- [x] MongoDB connection (`api/aria/db/mongodb.py`)
- [x] Pydantic models (`api/aria/db/models.py`)

### API Endpoints
- [x] `GET /api/v1/health` - Health check
- [x] `GET /api/v1/conversations` - List conversations
- [x] `POST /api/v1/conversations` - Create conversation
- [x] `GET /api/v1/conversations/{id}` - Get conversation
- [x] `DELETE /api/v1/conversations/{id}` - Delete conversation
- [x] `POST /api/v1/conversations/{id}/messages` - Send message (streaming)
- [x] `GET /api/v1/agents` - List agents
- [x] `POST /api/v1/agents` - Create agent
- [x] `GET /api/v1/agents/{id}` - Get agent

### LLM Integration
- [x] LLM adapter base class (`api/aria/llm/base.py`)
- [x] llama.cpp adapter (`api/aria/llm/llamacpp.py`)
- [x] LLM manager (`api/aria/llm/manager.py`)
- [x] Streaming working

### Agent Orchestrator
- [x] Basic orchestrator (`api/aria/core/orchestrator.py`)
- [x] Context assembly (no memory yet)
- [x] Response streaming
- [x] Conversation persistence

### CLI Client
- [x] CLI package setup (`cli/pyproject.toml`)
- [x] Chat command (`aria chat "message"`)
- [x] Conversation list command
- [x] Conversation continue command

### Default Configuration
- [x] Default agent created on first run
- [x] Settings collection initialized

### Testing
- [ ] Test infrastructure setup
- [ ] Health endpoint test
- [ ] Conversation CRUD tests
- [ ] llama.cpp adapter test (mocked)

---

## Phase Completion Criteria

**Phase 1 is DONE when:**
```bash
# 1. System starts cleanly
docker compose up -d
# All containers healthy

# 2. Can chat via CLI
aria chat "Hello!"
# Returns streaming response from LLM

# 3. Conversations persist
aria conversations list
# Shows previous conversations

# 4. Can continue conversation
aria chat --conversation <id> "What did I say before?"
# Includes conversation history
```

---

## Current Work

### Completed Since Phase 6
- **Phase 7**: Signal Integration — client, service, polling loop, message routing, voice note transcription
- **Phase 8**: Hardening — token counting, conversation summarization, resilience (retry + circuit breaker), DB migrations, usage tracking
- **Phase 9**: Mode system — agent switching via `/mode`, keyword detection, natural language patterns
- **Phase 10**: Memory categories — extraction types (fact/preference/event/skill/document)
- **Phase 12**: Coding sessions — subprocess management, watchdog, review service, backend registry (Claude Code, Codex)
- **Phase 13**: Task runner — background task submission, progress tracking, recovery handlers
- **Phase 19**: Security — audit logging, rate limiting, API key auth middleware
- **Dashboard**: 7-tab web dashboard (Modes, Memories, Research, Usage, Conversations, Workflows, Settings) with full CRUD (delete buttons for agents, memories, conversations, workflows)
- **CLI v0.2.0**: 20+ command groups covering all major features
- **Test suite**: 210 tests (unit + API integration) covering core components, command routing, API routes
- **Orchestrator refactor**: Command parsing extracted into `CommandRouter` class (`api/aria/core/commands.py`)
- **datetime migration**: All `datetime.utcnow()` and `datetime.now(datetime.UTC)` replaced with `datetime.now(timezone.utc)` across 25+ files
- **LLM token tracking**: OpenAI and OpenRouter adapters now request `stream_options: {include_usage: true}` for accurate streaming token counts
- **Workflow delete endpoint**: `DELETE /api/v1/workflows/{id}` added
- **MCP persistence**: MCP server configs saved to MongoDB `mcp_servers` collection, restored on app startup
- **Phase 14**: Scheduler — SchedulerService with tick loop, cron expressions, natural language reminder parsing, 6 API endpoints
- **Phase 15**: Widget quick actions panel (coding status, memories, research, new chat)
- **Phase 20**: ABP retirement — feature parity checklist with live service checks, data migration utilities, cutover validation endpoint

### Notes
- See `IMPLEMENTATION_PLAN.md` for detailed phase breakdown
- See `FUTURE_INVESTIGATIONS.md` for research topics not yet committed to

---

## File Inventory

### Created (Phase 1)
```
.gitignore
.env.example
docker-compose.yml
scripts/init-mongo.js

api/requirements.txt
api/Dockerfile
api/aria/__init__.py
api/aria/main.py
api/aria/config.py
api/aria/db/__init__.py
api/aria/db/mongodb.py
api/aria/db/models.py
api/aria/llm/__init__.py
api/aria/llm/base.py
api/aria/llm/llamacpp.py
api/aria/llm/manager.py
api/aria/core/__init__.py
api/aria/core/orchestrator.py
api/aria/api/__init__.py
api/aria/api/deps.py
api/aria/api/routes/__init__.py
api/aria/api/routes/health.py
api/aria/api/routes/conversations.py
api/aria/api/routes/agents.py

cli/pyproject.toml
cli/requirements.txt
cli/aria_cli/__init__.py
cli/aria_cli/main.py
```

### Created (Phase 2)
```
api/aria/memory/embeddings.py
api/aria/memory/short_term.py
api/aria/memory/long_term.py
api/aria/memory/extraction.py
api/aria/core/context.py
api/aria/api/routes/memories.py
```

### Created (Phase 3)
```
api/aria/tools/__init__.py
api/aria/tools/base.py
api/aria/tools/router.py
api/aria/tools/builtin/__init__.py
api/aria/tools/builtin/filesystem.py
api/aria/tools/builtin/shell.py
api/aria/tools/builtin/web.py
api/aria/tools/mcp/__init__.py
api/aria/tools/mcp/client.py
api/aria/tools/mcp/manager.py
api/aria/api/routes/tools.py
```

### Modified (Phase 2)
```
api/aria/main.py (added memory routes)
api/aria/core/orchestrator.py (integrated memory system)
cli/aria_cli/main.py (added memory commands)
PROJECT_STATUS.md (this file)
```

### Created (Phase 4)
```
api/aria/llm/anthropic.py
api/aria/llm/openai.py
```

### Modified (Phase 3)
```
api/aria/main.py (added tools routes, tool initialization, MCP shutdown)
api/aria/core/orchestrator.py (integrated tool system, tool call handling)
api/aria/api/deps.py (added tool_router and mcp_manager dependencies)
cli/aria_cli/main.py (added tools and mcp commands)
PROJECT_STATUS.md (this file)
```

### Modified (Phase 4)
```
api/aria/llm/manager.py (added Anthropic and OpenAI adapter support, backend availability check)
api/aria/core/orchestrator.py (added LLM fallback chain logic)
api/aria/api/routes/health.py (added LLM status endpoint)
api/requirements.txt (already included anthropic and openai packages)
PROJECT_STATUS.md (this file)
```

---

## Recent Decisions

| Date | Decision | Rationale |
|------|----------|-----------|
| 2025-12-06 | Use official Anthropic and OpenAI SDKs | Better reliability and maintenance than custom implementation |
| 2025-12-06 | Implement fallback chain in orchestrator | Automatic failover to cloud LLMs when local fails |
| 2025-12-06 | Add LLM backend availability check | Help users diagnose configuration issues |
| 2025-12-06 | Implement tool sandboxing for filesystem | Limit file operations to allowed paths for security |
| 2025-12-06 | Use stdio transport for MCP client | Simplest and most compatible MCP transport method |
| 2025-12-06 | Separate built-in and MCP tools | Clear distinction between native and external tools |
| 2025-12-06 | Tool execution in orchestrator | Allows LLM to use tools during conversations |
| 2025-11-29 | Use MongoDB 8.2 + mongot instead of Atlas | Self-hosted vector search without Atlas subscription |
| 2025-11-29 | Implement Phase 1 completely before testing | Get full stack working before integration testing |
| 2025-11-29 | Use Rich library for CLI | Better terminal UX with colors and formatting |
| 2025-11-29 | Hybrid search with RRF fusion | Best of both lexical and semantic search |
| 2025-11-29 | LLM-based memory extraction | More flexible than rule-based extraction |
| 2025-11-29 | Background memory extraction | Don't block chat responses |

---

## Known Issues

| Issue | Severity | Status |
|-------|----------|--------|
| Some route files still use `datetime.now(datetime.UTC)` (class-level) — works in Python 3.13+ but needs shim in 3.12 | Low | Mitigated (test shim in place) |

---

## Next Actions

1. **Integration test** Signal, Research, and Workflow systems with live services
2. **End-to-end test** Scheduler with live MongoDB
3. **Run ABP migration** when ready to retire AgentBenchPlatform

---

## Phase Summary

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Foundation (API, LLM, Conversations) | COMPLETE |
| 2 | Memory System (Short-term, Long-term, Embeddings) | COMPLETE |
| 3 | Tools & MCP (Built-in tools, MCP client) | COMPLETE |
| 4 | Cloud LLM Adapters (Anthropic, OpenAI, OpenRouter, Fallback) | COMPLETE |
| 5 | Web UI (Next.js) | COMPLETE |
| 6 | Desktop Widget + llama.cpp ROCm + Voice I/O | COMPLETE |
| 7 | Signal Integration (mobile interface) | COMPLETE |
| 8 | Hardening (tokens, summaries, resilience, migrations, usage) | COMPLETE |
| 9 | Mode System (agent switching, keywords) | COMPLETE |
| 10 | Memory Categories (extraction types) | COMPLETE |
| 11 | Research System (recursive web research) | COMPLETE (needs integration testing) |
| 12 | Coding Sessions (subprocess, watchdog, review) | COMPLETE |
| 13 | Task Runner (background tasks, recovery) | COMPLETE |
| 14 | Scheduler (cron tasks, reminders) | COMPLETE |
| 15 | Widget Enhancements (mode switcher, voice, quick actions) | COMPLETE |
| 16 | Web UI Dashboard (7-tab management console) | COMPLETE |
| 17 | CLI Enhancements (20+ command groups) | COMPLETE |
| 18 | Workflow Engine (multi-step orchestration) | COMPLETE |
| 19 | Security (audit, rate limiting, API auth) | COMPLETE |
| 20 | Retire ABP (migration, cutover validation) | COMPLETE |
